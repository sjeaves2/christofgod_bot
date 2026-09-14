"""ICS (iCalendar) file generation for events and appointments."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

import pytz
from icalendar import Alarm, Calendar, Event, vText


_ALARM_OFFSETS: list[tuple[int, str]] = [
    (120, "2 hours"),
    (60, "1 hour"),
]


def _add_alarms(vevent: Event, offsets: list[tuple[int, str]] = _ALARM_OFFSETS) -> None:
    """Attach VALARM (DISPLAY) components for each offset (minutes before start)."""
    for minutes, label in offsets:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", f"Reminder: {label} until event")
        alarm.add("trigger", timedelta(minutes=-minutes))
        vevent.add_component(alarm)


def _make_calendar(name: str) -> Calendar:
    cal = Calendar()
    cal.add("prodid", f"-//Christ of God Ministries Bot//{name}//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", vText(name))
    return cal


def _event_uid(evt: dict[str, Any]) -> str:
    """Stable UID derived from the event's own key.

    Calendar clients use UID to decide whether an imported entry is new or an
    update of one already held: same UID replaces, different UID adds a second
    copy. This used to be a fresh uuid4() per export, so exporting twice left
    the congregation with every service duplicated in their own calendars.

    event["key"] is already the right handle — it is what notification state is
    tracked by, so it is stable across restarts and unique per service (it
    carries the date, and for a combined Sabbath+festival service the sorted
    component keys). Falls back to a name+timestamp digest only if some caller
    supplies an event with no key at all.
    """
    key = evt.get("key")
    if not key:
        svc = evt.get("service_time")
        stamp = svc.isoformat() if hasattr(svc, "isoformat") else str(svc)
        raw = f"{evt.get('name', 'event')}|{stamp}"
        key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{key}@christofgodbot"


def events_to_ics(events: list[dict[str, Any]],
                  calendar_name: str = "Christ of God Ministries Events") -> bytes:
    """Convert a list of event dicts to ICS bytes."""
    cal = _make_calendar(calendar_name)
    for evt in events:
        vevent = Event()
        vevent.add("summary", evt.get("name", "Event"))
        svc_time: datetime = evt["service_time"]
        duration = timedelta(minutes=int(evt.get("duration_minutes", 60)))
        vevent.add("dtstart", svc_time)
        vevent.add("dtend", svc_time + duration)
        vevent.add("uid", _event_uid(evt))
        # Lets a client treat a re-import as an update (changed time or join
        # link) rather than a conflict, as the appointment exports already do.
        vevent.add("sequence", int(evt.get("sequence", 0)))
        _add_alarms(vevent)
        if evt.get("url"):
            vevent.add("url", evt["url"])
        if evt.get("description"):
            vevent.add("description", evt["description"])
        if evt.get("announcements"):
            note = "\n".join(evt["announcements"])
            existing = vevent.get("description", "")
            vevent["description"] = (str(existing) + "\n\n" + note).strip() if existing else note
        cal.add_component(vevent)
    return cal.to_ical()


def _appointment_uid(appt: dict[str, Any]) -> str:
    """Stable UID so a later cancellation matches the original event."""
    return f"appt-{appt['id']}@christofgodbot"


def _appointment_datetime(appt: dict[str, Any], tz: pytz.BaseTzInfo) -> datetime:
    """Return the confirmed datetime, localising if it is naive.

    Accepts either a datetime or an ISO-8601 string.
    """
    appt_dt = appt["confirmed_datetime"]
    if isinstance(appt_dt, str):
        appt_dt = datetime.fromisoformat(appt_dt)
    if appt_dt.tzinfo is None:
        appt_dt = tz.localize(appt_dt)
    return appt_dt


def appointment_to_ics(
    appt: dict[str, Any],
    tz: pytz.BaseTzInfo,
    organizer_name: str = "Christ of God Ministries Bot",
) -> bytes:
    """Generate a single-event ICS for a confirmed appointment."""
    cal = _make_calendar("Appointment")
    cal.add("method", "REQUEST")
    vevent = Event()
    vevent.add("summary", f"Meeting with {appt['official_name']}")

    appt_dt = _appointment_datetime(appt, tz)
    duration = timedelta(minutes=int(appt.get("duration_minutes", 30)))
    vevent.add("dtstart", appt_dt)
    vevent.add("dtend", appt_dt + duration)
    vevent.add("uid", _appointment_uid(appt))
    vevent.add("sequence", 0)
    vevent.add("status", "CONFIRMED")
    vevent.add("description", appt.get("description", ""))
    _add_alarms(vevent)
    cal.add_component(vevent)
    return cal.to_ical()


def appointment_cancellation_to_ics(
    appt: dict[str, Any],
    tz: pytz.BaseTzInfo,
    organizer_name: str = "Christ of God Ministries Bot",
) -> bytes:
    """Generate a cancellation ICS for a previously confirmed appointment.

    Uses METHOD:CANCEL, STATUS:CANCELLED, the same UID as the original event
    and an incremented SEQUENCE so calendar clients remove the event.
    """
    cal = _make_calendar("Appointment")
    cal.add("method", "CANCEL")
    vevent = Event()
    vevent.add("summary", f"Meeting with {appt['official_name']}")

    appt_dt = _appointment_datetime(appt, tz)
    duration = timedelta(minutes=int(appt.get("duration_minutes", 30)))
    vevent.add("dtstart", appt_dt)
    vevent.add("dtend", appt_dt + duration)
    vevent.add("uid", _appointment_uid(appt))
    vevent.add("sequence", int(appt.get("sequence", 0)) + 1)
    vevent.add("status", "CANCELLED")
    vevent.add("description", appt.get("description", ""))
    cal.add_component(vevent)
    return cal.to_ical()

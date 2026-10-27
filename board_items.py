"""Board compliance items: corporate obligations with renewal deadlines.

Logo registration, tax-exemption renewal, corporate filings — things that have
a DUE DATE, a lead time, and consequences for being missed. Board officers add
them, the bot nags until they are marked complete, and completing one rolls it
forward to the next cycle rather than retiring it, because most of these repeat
every N years.

Pure domain logic: dates, ordering, the notification schedule, and the
lifecycle transitions. No Telegram, no I/O — storage holds the file, the
handler holds the conversation.

Everything here works in `datetime.date`, never datetime. That is deliberate.
These are all-day obligations with no time of day, and pytz arithmetic on a
localized datetime carries TODAY's UTC offset across a DST boundary — so
"60 days before 2027-03-14" computed from an aware datetime in November is off
by an hour, and an hour is enough to move a date. Dates have no offset to get
wrong.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

# Default lead time: the first notice goes out this many days before the due
# date. Per-item, because a trademark renewal wants more warning than a permit.
NOTIFY_DAYS_DEFAULT = 60

# Inside this many days of the due date, notices go out EVERY day. Outside it
# (but inside the lead time) they go out weekly.
DAILY_WINDOW_DAYS = 21
WEEKLY_INTERVAL_DAYS = 7

# Lifecycle. "deleted" rather than a real removal: a mis-tapped Delete on the
# tax-exemption renewal should be recoverable, and the daily maintenance job
# purges these after the retention window below.
STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"
STATUS_DELETED = "deleted"

DELETED_PURGE_AFTER_DAYS = 90

ACTION_MAX = 120
DESCRIPTION_MAX = 1024


def new_id() -> str:
    """A short opaque id, matching the announcement convention."""
    return uuid.uuid4().hex[:8].upper()


# ---------------------------------------------------------------------------
# Field access
# ---------------------------------------------------------------------------


def parse_date(value: Any) -> date | None:
    """Parse a YYYY-MM-DD string (or pass a date through); None if unusable.

    Tolerant on purpose. board_items.yaml is hand-editable, and YAML parses an
    unquoted 2027-03-14 into a date object all by itself — so this has to accept
    both what the bot writes and what ruamel/PyYAML hand back.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def due_date(item: dict[str, Any]) -> date | None:
    return parse_date(item.get("due"))


def notify_days(item: dict[str, Any]) -> int:
    """Lead time for this item, falling back to the default.

    A hand-edited file can hold a string, a negative number or nonsense; any of
    those would otherwise silently disable or invert the schedule.
    """
    try:
        value = int(item.get("notify_days", NOTIFY_DAYS_DEFAULT))
    except (TypeError, ValueError):
        return NOTIFY_DAYS_DEFAULT
    return value if value > 0 else NOTIFY_DAYS_DEFAULT


def status_of(item: dict[str, Any]) -> str:
    """The item's status, defaulting to pending.

    Absent means pending: an item someone added by hand without a status field
    is an obligation that has not been met, and treating it as complete would
    silence the bot about it.
    """
    value = str(item.get("status") or "").strip().lower()
    return value if value in (STATUS_PENDING, STATUS_COMPLETED, STATUS_DELETED) \
        else STATUS_PENDING


def days_until(item: dict[str, Any], today: date) -> int | None:
    """Whole days from *today* to the due date; negative once overdue."""
    due = due_date(item)
    return None if due is None else (due - today).days


# ---------------------------------------------------------------------------
# Selection and ordering
# ---------------------------------------------------------------------------


def _sort_key(item: dict[str, Any]) -> tuple[int, date, str]:
    """Due date ascending, undated items last, ties broken by action text."""
    due = due_date(item)
    return (1, date.max, str(item.get("action", ""))) if due is None \
        else (0, due, str(item.get("action", "")))


def pending(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Items still to be done, soonest first."""
    return sorted((i for i in items if status_of(i) == STATUS_PENDING), key=_sort_key)


def completed(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retired items, most recently completed first — the reopen candidates."""
    return sorted((i for i in items if status_of(i) == STATUS_COMPLETED),
                  key=lambda i: str(i.get("completed_at") or ""), reverse=True)


def by_id(items: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    return next((i for i in items if str(i.get("id")) == str(item_id)), None)


# ---------------------------------------------------------------------------
# The notification schedule
# ---------------------------------------------------------------------------


def notice_due(item: dict[str, Any], today: date) -> bool:
    """Whether a notice should go out for *item* today.

    The schedule the user specified: one notice at the lead time, then weekly,
    then every day once inside DAILY_WINDOW_DAYS.

    Two properties worth stating, because they are the reason `last_notified`
    lives on the item rather than in a separate state file:

    * **It is self-healing.** If the bot was down for three days, the next run
      sees a stale `last_notified` and sends — there is no missed slot to catch
      up, because the decision is recomputed from today's date every time.
    * **It cannot double-send.** Two runs in one day (a restart at 06:05) see
      `last_notified == today` and the second does nothing.

    An OVERDUE item keeps notifying daily, deliberately. A tax exemption does
    not stop mattering on the day it lapses, and going quiet exactly when the
    deadline passes is the one behaviour that would make this feature harmful.
    """
    if status_of(item) != STATUS_PENDING:
        return False
    days = days_until(item, today)
    if days is None or days > notify_days(item):
        return False
    last = parse_date(item.get("last_notified"))
    if last == today:
        return False
    if days <= DAILY_WINDOW_DAYS:
        return True
    # Outside the daily window: weekly. `last is None` covers the first notice,
    # whether that falls exactly on the lead-time day or later — an item added
    # when it is already 40 days out (lead time 60) is overdue for its first
    # notice, and gets it on the next run rather than waiting a week.
    return last is None or (today - last).days >= WEEKLY_INTERVAL_DAYS


def mark_notified(item: dict[str, Any], today: date) -> None:
    item["last_notified"] = today.isoformat()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def complete(item: dict[str, Any], today: date, by: str) -> None:
    """Mark *item* done, pushing the cycle just finished into its history.

    The item keeps its id, action, description and url, so reopening it later
    with a new due date is the whole of "renew this again in three years".
    That is why there is no separate "create from template" step.
    """
    item.setdefault("history", []).append({
        "due": due_date(item).isoformat() if due_date(item) else str(item.get("due")),
        "completed_at": today.isoformat(),
        "completed_by": by,
    })
    item["status"] = STATUS_COMPLETED
    item["completed_at"] = today.isoformat()
    item["completed_by"] = by
    # Clear the send marker so a reopened item's first notice is not suppressed
    # by a `last_notified` left over from the previous cycle.
    item.pop("last_notified", None)


def sequence_of(item: dict[str, Any]) -> int:
    """The item's ICS revision number.

    Calendar clients use UID to decide whether an imported entry replaces one
    they already hold, and SEQUENCE to decide whether the incoming copy is
    NEWER. Without a bump, a client that already has last year's version of an
    item may keep showing the old date after a fresh export — the update is
    silently ignored as a duplicate.
    """
    try:
        return max(0, int(item.get("sequence", 0)))
    except (TypeError, ValueError):
        return 0


def bump_sequence(item: dict[str, Any]) -> None:
    """Record that this item's dates changed, for the benefit of ICS clients."""
    item["sequence"] = sequence_of(item) + 1


def reopen(item: dict[str, Any], new_due: date) -> None:
    """Put a completed item back in the queue with a new due date."""
    item["status"] = STATUS_PENDING
    item["due"] = new_due.isoformat()
    bump_sequence(item)
    item.pop("completed_at", None)
    item.pop("completed_by", None)
    item.pop("last_notified", None)


def soft_delete(item: dict[str, Any], today: date, by: str) -> None:
    item["status"] = STATUS_DELETED
    item["deleted_at"] = today.isoformat()
    item["deleted_by"] = by


def purge_deleted(items: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Drop soft-deleted items older than the retention window.

    An unparseable `deleted_at` is KEPT, never silently discarded — the same
    rule the announcement purge follows.
    """
    cutoff = today - timedelta(days=DELETED_PURGE_AFTER_DAYS)
    keep = []
    for item in items:
        if status_of(item) != STATUS_DELETED:
            keep.append(item)
            continue
        when = parse_date(item.get("deleted_at"))
        if when is None or when >= cutoff:
            keep.append(item)
    return keep


# ---------------------------------------------------------------------------
# ICS alarm offsets
# ---------------------------------------------------------------------------

#: Lead times (in days) for the calendar alarms, in the order a client shows
#: them. Deliberately NOT a mirror of the bot's schedule: mirroring it exactly
#: would mean ~28 discrete VALARMs per item, because a VALARM has one trigger
#: and ICS has no "weekly then daily" rule to express. Five is enough for a
#: phone to be useful; the bot remains the authoritative nag.
ICS_ALARM_DAYS = (60, 30, 14, 7, 1)


def ics_alarm_offsets(item: dict[str, Any]) -> list[int]:
    """Alarm lead times for this item, longest first.

    Never earlier than the item's own lead time — an alarm 60 days out on an
    item the board asked to hear about at 30 would contradict the setting. The
    lead time itself is always included, so a custom 45 still gets a notice on
    day 45.
    """
    lead = notify_days(item)
    offsets = {d for d in ICS_ALARM_DAYS if d <= lead}
    offsets.add(lead)
    return sorted(offsets, reverse=True)

"""board_items — the notification schedule and the lifecycle transitions.

The schedule is the part worth testing hardest: it is the whole feature. An
item that goes quiet, double-sends, or stops nagging the day a deadline passes
is worse than no feature at all, because the board would be relying on it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import board_items as bi


def item(**over):
    base = {"id": "X1", "action": "Renew something", "due": "2027-03-14",
            "notify_days": 60, "status": "pending"}
    base.update(over)
    return base


def days_before(due: str, n: int) -> date:
    return bi.parse_date(due) - timedelta(days=n)


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------


class TestFieldParsing:
    def test_parses_a_string_date(self):
        assert bi.parse_date("2027-03-14") == date(2027, 3, 14)

    def test_passes_a_date_through(self):
        """YAML turns an unquoted 2027-03-14 into a date all by itself."""
        assert bi.parse_date(date(2027, 3, 14)) == date(2027, 3, 14)

    def test_rejects_nonsense(self):
        for bad in ("", "soon", "14/03/2027", None, 42):
            assert bi.parse_date(bad) is None, bad

    def test_notify_days_defaults_when_absent(self):
        assert bi.notify_days({}) == bi.NOTIFY_DAYS_DEFAULT

    @pytest.mark.parametrize("bad", ["", "sixty", None, 0, -5])
    def test_notify_days_rejects_unusable_values(self, bad):
        """0 or a negative lead time would disable or invert the schedule."""
        assert bi.notify_days({"notify_days": bad}) == bi.NOTIFY_DAYS_DEFAULT

    def test_notify_days_accepts_a_numeric_string(self):
        assert bi.notify_days({"notify_days": "30"}) == 30

    def test_absent_status_is_pending_not_completed(self):
        """An item added by hand with no status must still be nagged about."""
        assert bi.status_of({}) == bi.STATUS_PENDING

    def test_unrecognised_status_is_pending(self):
        assert bi.status_of({"status": "whatever"}) == bi.STATUS_PENDING

    def test_status_is_case_insensitive(self):
        assert bi.status_of({"status": "Completed"}) == bi.STATUS_COMPLETED


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_pending_is_sorted_by_due_date(self):
        items = [item(id="C", due="2027-06-01"), item(id="A", due="2027-01-05"),
                 item(id="B", due="2027-03-14")]
        assert [i["id"] for i in bi.pending(items)] == ["A", "B", "C"]

    def test_pending_excludes_completed_and_deleted(self):
        items = [item(id="P"), item(id="C", status="completed"),
                 item(id="D", status="deleted")]
        assert [i["id"] for i in bi.pending(items)] == ["P"]

    def test_an_undated_item_sorts_last_rather_than_crashing(self):
        items = [item(id="NODATE", due="later"), item(id="DATED", due="2027-03-14")]
        assert [i["id"] for i in bi.pending(items)] == ["DATED", "NODATE"]

    def test_completed_is_newest_first(self):
        items = [item(id="OLD", status="completed", completed_at="2020-01-01"),
                 item(id="NEW", status="completed", completed_at="2026-01-01")]
        assert [i["id"] for i in bi.completed(items)] == ["NEW", "OLD"]

    def test_by_id_finds_an_item(self):
        items = [item(id="A"), item(id="B")]
        assert bi.by_id(items, "B")["id"] == "B"
        assert bi.by_id(items, "ZZ") is None


# ---------------------------------------------------------------------------
# The notification schedule
# ---------------------------------------------------------------------------


class TestNoticeSchedule:
    def test_silent_before_the_lead_time(self):
        """61 days out, lead time 60 — nothing yet."""
        assert bi.notice_due(item(), days_before("2027-03-14", 61)) is False

    def test_fires_on_the_lead_time_day(self):
        assert bi.notice_due(item(), days_before("2027-03-14", 60)) is True

    def test_weekly_outside_the_daily_window(self):
        today = days_before("2027-03-14", 53)   # 7 days after the first notice
        sent_last_week = item(last_notified=days_before("2027-03-14", 60).isoformat())
        assert bi.notice_due(sent_last_week, today) is True

    def test_silent_between_weekly_notices(self):
        today = days_before("2027-03-14", 57)   # only 3 days since the last
        sent = item(last_notified=days_before("2027-03-14", 60).isoformat())
        assert bi.notice_due(sent, today) is False

    def test_daily_inside_twenty_one_days(self):
        """Yesterday's notice must not suppress today's inside the window."""
        today = days_before("2027-03-14", 20)
        sent_yesterday = item(last_notified=days_before("2027-03-14", 21).isoformat())
        assert bi.notice_due(sent_yesterday, today) is True

    def test_the_boundary_day_is_inside_the_daily_window(self):
        today = days_before("2027-03-14", bi.DAILY_WINDOW_DAYS)
        sent_yesterday = item(
            last_notified=days_before("2027-03-14", bi.DAILY_WINDOW_DAYS + 1).isoformat())
        assert bi.notice_due(sent_yesterday, today) is True

    def test_the_day_outside_the_boundary_is_weekly(self):
        today = days_before("2027-03-14", bi.DAILY_WINDOW_DAYS + 1)
        sent_yesterday = item(
            last_notified=days_before("2027-03-14", bi.DAILY_WINDOW_DAYS + 2).isoformat())
        assert bi.notice_due(sent_yesterday, today) is False

    def test_never_twice_in_one_day(self):
        """A restart at 06:05 must not re-send the 06:00 notice."""
        today = days_before("2027-03-14", 5)
        assert bi.notice_due(item(last_notified=today.isoformat()), today) is False

    def test_self_heals_after_an_outage(self):
        """Three days down inside the daily window — the next run still sends."""
        today = days_before("2027-03-14", 10)
        stale = item(last_notified=days_before("2027-03-14", 13).isoformat())
        assert bi.notice_due(stale, today) is True

    def test_keeps_notifying_once_overdue(self):
        """The one behaviour that would make this feature harmful is going
        quiet exactly when the deadline passes."""
        overdue = item(last_notified="2027-03-14")
        assert bi.notice_due(overdue, date(2027, 3, 20)) is True

    def test_first_notice_is_immediate_for_an_item_added_inside_its_lead_time(self):
        """Added 40 days out with a 60-day lead time: the lead-time day is
        already past, so the notice is owed now, not next week."""
        fresh = item()   # no last_notified
        assert bi.notice_due(fresh, days_before("2027-03-14", 40)) is True

    def test_completed_items_are_silent(self):
        done = item(status="completed")
        assert bi.notice_due(done, days_before("2027-03-14", 5)) is False

    def test_deleted_items_are_silent(self):
        assert bi.notice_due(item(status="deleted"),
                             days_before("2027-03-14", 5)) is False

    def test_an_undated_item_is_silent_rather_than_crashing(self):
        assert bi.notice_due(item(due="whenever"), date(2027, 1, 1)) is False

    def test_a_short_lead_time_is_honoured(self):
        short = item(notify_days=30)
        assert bi.notice_due(short, days_before("2027-03-14", 31)) is False
        assert bi.notice_due(short, days_before("2027-03-14", 30)) is True

    def test_mark_notified_stamps_today(self):
        rec = item()
        bi.mark_notified(rec, date(2027, 1, 15))
        assert rec["last_notified"] == "2027-01-15"

    def test_the_schedule_crosses_a_dst_boundary_correctly(self):
        """Dates, not datetimes: pytz arithmetic on an aware datetime carries
        today's UTC offset across a DST change, and an hour is enough to move a
        date. 2027-03-14 is the US DST switch; the lead-time day for a 60-day
        notice is 2027-01-13, which must be exact."""
        assert days_before("2027-03-14", 60) == date(2027, 1, 13)
        assert bi.notice_due(item(), date(2027, 1, 13)) is True
        assert bi.notice_due(item(), date(2027, 1, 12)) is False


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestCompletion:
    def test_completing_records_the_cycle_in_history(self):
        rec = item()
        bi.complete(rec, date(2027, 2, 1), "Samuel Eaves")
        assert rec["history"] == [{"due": "2027-03-14",
                                   "completed_at": "2027-02-01",
                                   "completed_by": "Samuel Eaves"}]

    def test_completing_sets_status_and_who(self):
        rec = item()
        bi.complete(rec, date(2027, 2, 1), "Samuel Eaves")
        assert bi.status_of(rec) == bi.STATUS_COMPLETED
        assert rec["completed_by"] == "Samuel Eaves"

    def test_completing_keeps_the_details_for_the_next_cycle(self):
        rec = item(description="Serial 123", url="https://example.test/x")
        bi.complete(rec, date(2027, 2, 1), "X")
        assert rec["description"] == "Serial 123"
        assert rec["url"] == "https://example.test/x"
        assert rec["id"] == "X1"

    def test_completing_twice_keeps_both_cycles(self):
        rec = item()
        bi.complete(rec, date(2024, 2, 1), "A")
        bi.reopen(rec, date(2027, 3, 14))
        bi.complete(rec, date(2027, 2, 1), "B")
        assert [h["completed_at"] for h in rec["history"]] == ["2024-02-01", "2027-02-01"]


class TestReopen:
    def test_reopening_makes_it_pending_with_the_new_date(self):
        rec = item()
        bi.complete(rec, date(2027, 2, 1), "X")
        bi.reopen(rec, date(2030, 3, 14))
        assert bi.status_of(rec) == bi.STATUS_PENDING
        assert bi.due_date(rec) == date(2030, 3, 14)

    def test_reopening_clears_the_completion_fields(self):
        rec = item()
        bi.complete(rec, date(2027, 2, 1), "X")
        bi.reopen(rec, date(2030, 3, 14))
        assert "completed_at" not in rec
        assert "completed_by" not in rec

    def test_a_reopened_item_notifies_again(self):
        """A last_notified left from the previous cycle would suppress the whole
        of the next one — the item would sit silent until its due date."""
        rec = item(last_notified="2027-03-01")
        bi.complete(rec, date(2027, 3, 2), "X")
        bi.reopen(rec, date(2030, 3, 14))
        assert bi.notice_due(rec, date(2030, 1, 13)) is True

    def test_reopen_clears_the_send_marker_by_itself(self):
        """Not reached through complete(). The test above passes even if reopen
        stops clearing last_notified, because complete() clears it too — so it
        proves nothing about reopen. This record never went through complete():
        a hand-edited file, or an item completed before that pop existed."""
        stale = item(status="completed", last_notified="2029-12-01")
        bi.reopen(stale, date(2030, 3, 14))
        assert "last_notified" not in stale
        assert bi.notice_due(stale, date(2030, 1, 13)) is True


class TestSoftDelete:
    def test_deleting_hides_it_without_losing_it(self):
        rec = item()
        bi.soft_delete(rec, date(2027, 1, 1), "X")
        assert bi.status_of(rec) == bi.STATUS_DELETED
        assert bi.pending([rec]) == []
        assert rec["action"] == "Renew something"

    def test_purge_drops_an_old_deletion(self):
        old = item(id="OLD", status="deleted", deleted_at="2026-01-01")
        assert bi.purge_deleted([old], date(2027, 1, 1)) == []

    def test_purge_keeps_a_recent_deletion(self):
        recent = item(id="R", status="deleted", deleted_at="2026-12-20")
        assert [i["id"] for i in bi.purge_deleted([recent], date(2027, 1, 1))] == ["R"]

    def test_purge_keeps_pending_items(self):
        assert len(bi.purge_deleted([item()], date(2027, 1, 1))) == 1

    def test_purge_keeps_an_unparseable_deletion_date(self):
        """Never silently discard a record the parser could not read."""
        broken = item(id="B", status="deleted", deleted_at="whenever")
        assert [i["id"] for i in bi.purge_deleted([broken], date(2027, 1, 1))] == ["B"]


# ---------------------------------------------------------------------------
# ICS alarm offsets
# ---------------------------------------------------------------------------


class TestIcsAlarmOffsets:
    def test_default_lead_time_gets_the_full_ladder(self):
        assert bi.ics_alarm_offsets(item()) == [60, 30, 14, 7, 1]

    def test_no_alarm_earlier_than_the_items_own_lead_time(self):
        assert bi.ics_alarm_offsets(item(notify_days=30)) == [30, 14, 7, 1]

    def test_a_custom_lead_time_is_always_included(self):
        assert bi.ics_alarm_offsets(item(notify_days=45)) == [45, 30, 14, 7, 1]

    def test_a_very_short_lead_time_still_gets_one_alarm(self):
        assert bi.ics_alarm_offsets(item(notify_days=3)) == [3, 1]

    def test_the_count_stays_small(self):
        """The alternative considered was mirroring the bot exactly, which is
        ~28 VALARMs per item. If this ever grows, it was a decision."""
        assert len(bi.ics_alarm_offsets(item())) <= 6


class TestIds:
    def test_ids_are_short_and_distinct(self):
        ids = {bi.new_id() for _ in range(200)}
        assert len(ids) == 200
        assert all(len(i) == 8 and i.isalnum() for i in ids)

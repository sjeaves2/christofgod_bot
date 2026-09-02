"""Tests for /stats — argument grammar, activity-log parsing, and both views."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.stats as st

TZ = pytz.timezone("America/New_York")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _log_line(when: datetime, kind: str, detail: str, who: str = "system") -> str:
    return f"[{when.strftime('%Y-%m-%d %H:%M:%S %Z')}] [{kind}] {who} | {detail}\n"


def _write_log(tmp_path, lines) -> Path:
    p = tmp_path / "bot_activity.log"
    p.write_text("".join(lines), encoding="utf-8")
    return p


def _upd(chat_id: int = 1) -> MagicMock:
    upd = MagicMock()
    upd.effective_user.id = chat_id
    upd.effective_user.username = "admin"
    upd.effective_user.full_name = "Admin"
    upd.message.reply_text = AsyncMock()
    return upd


# ---------------------------------------------------------------------------
# Argument grammar
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_default_is_system_seven_days(self):
        assert st.parse_args([]) == ("system", 7, False)
        assert st.parse_args(None) == ("system", 7, False)

    def test_bare_number_sets_period(self):
        assert st.parse_args(["14"]) == ("system", 14, False)

    def test_usage_keyword_switches_view(self):
        assert st.parse_args(["usage"]) == ("usage", 7, False)

    def test_usage_with_period(self):
        assert st.parse_args(["usage", "30"]) == ("usage", 30, False)

    def test_order_does_not_matter(self):
        assert st.parse_args(["30", "usage"]) == ("usage", 30, False)

    def test_case_insensitive(self):
        assert st.parse_args(["USAGE"]) == ("usage", 7, False)

    def test_period_clamped_to_max(self):
        view, days, clamped = st.parse_args(["90"])
        assert (view, days, clamped) == ("system", 30, True)

    def test_zero_becomes_one_day(self):
        assert st.parse_args(["0"])[1] == 1

    def test_unknown_token_falls_back_to_defaults(self):
        assert st.parse_args(["nonsense"]) == ("system", 7, False)

    def test_explicit_system_keyword(self):
        assert st.parse_args(["system", "10"]) == ("system", 10, False)


# ---------------------------------------------------------------------------
# Activity-log parsing
# ---------------------------------------------------------------------------

class TestReadActivity:
    def test_parses_real_log_format(self, tmp_path):
        now = datetime.now(TZ)
        p = _write_log(tmp_path, [
            _log_line(now - timedelta(hours=1), "COMMAND", "/start", "Sam @sjeaves2 (id:1)"),
            _log_line(now - timedelta(hours=2), "NOTIFICATION",
                      "Sent notification for 'Sabbath Eve' to 3 user(s)"),
        ])
        with patch.object(st, "ACTIVITY_LOG", p):
            entries = st.read_activity(7, now)
        assert len(entries) == 2
        assert {e["kind"] for e in entries} == {"COMMAND", "NOTIFICATION"}

    def test_entries_outside_window_excluded(self, tmp_path):
        now = datetime.now(TZ)
        p = _write_log(tmp_path, [
            _log_line(now - timedelta(days=1), "COMMAND", "/recent"),
            _log_line(now - timedelta(days=40), "COMMAND", "/ancient"),
        ])
        with patch.object(st, "ACTIVITY_LOG", p):
            entries = st.read_activity(7, now)
        assert [e["detail"] for e in entries] == ["/recent"]

    def test_malformed_lines_skipped(self, tmp_path):
        now = datetime.now(TZ)
        p = _write_log(tmp_path, [
            "this line is garbage\n",
            "[not-a-date] [COMMAND] x | /nope\n",
            _log_line(now, "COMMAND", "/good"),
        ])
        with patch.object(st, "ACTIVITY_LOG", p):
            entries = st.read_activity(7, now)
        assert [e["detail"] for e in entries] == ["/good"]

    def test_missing_log_returns_empty(self, tmp_path):
        with patch.object(st, "ACTIVITY_LOG", tmp_path / "absent.log"):
            assert st.read_activity(7) == []


# ---------------------------------------------------------------------------
# System view
# ---------------------------------------------------------------------------

class TestSystemReport:
    def _report(self, tmp_path, lines, days=7, clamped=False, now=None):
        now = now or datetime.now(TZ)
        p = _write_log(tmp_path, lines)
        with patch.object(st, "ACTIVITY_LOG", p):
            return st.build_system_report(days, clamped, now)

    def test_counts_errors_in_24h_and_period(self, tmp_path):
        now = datetime.now(TZ)
        lines = [_log_line(now - timedelta(hours=2), "ERROR", "ERR-AAA111 ValueError: boom"),
                 _log_line(now - timedelta(days=3), "ERROR", "ERR-BBB222 KeyError: nope")]
        text = self._report(tmp_path, lines, now=now)
        assert "*Errors — last 24h:* 1" in text
        assert "*Errors — last 7d:* 2" in text

    def test_groups_by_exception_type(self, tmp_path):
        now = datetime.now(TZ)
        lines = [_log_line(now, "ERROR", "ERR-1 ValueError: a"),
                 _log_line(now, "ERROR", "ERR-2 ValueError: b"),
                 _log_line(now, "ERROR", "ERR-3 KeyError: c")]
        text = self._report(tmp_path, lines, now=now)
        assert "ValueError — 2" in text
        assert "KeyError — 1" in text

    def test_shows_most_recent_error(self, tmp_path):
        now = datetime.now(TZ)
        lines = [_log_line(now - timedelta(days=2), "ERROR", "ERR-OLD ValueError: old"),
                 _log_line(now - timedelta(minutes=5), "ERROR", "ERR-NEW KeyError: newest")]
        text = self._report(tmp_path, lines, now=now)
        assert "ERR-NEW" in text

    def test_includes_uptime(self, tmp_path):
        text = self._report(tmp_path, [])
        assert "*Uptime:*" in text

    def test_reports_clamping(self, tmp_path):
        text = self._report(tmp_path, [], days=30, clamped=True)
        assert "capped at 30 days" in text

    def test_clean_period_has_no_error_section(self, tmp_path):
        now = datetime.now(TZ)
        text = self._report(tmp_path, [_log_line(now, "COMMAND", "/events")], now=now)
        assert "*Errors — last 24h:* 0" in text
        assert "Most frequent" not in text


# ---------------------------------------------------------------------------
# Usage view
# ---------------------------------------------------------------------------

class TestUsageReport:
    def _report(self, tmp_path, lines, users=None, appts=None, anns=None,
                days=7, clamped=False, now=None):
        now = now or datetime.now(TZ)
        p = _write_log(tmp_path, lines)

        async def _users():
            return users if users is not None else []

        async def _appts():
            return appts if appts is not None else []

        async def _anns():
            return anns if anns is not None else []

        with patch.object(st, "ACTIVITY_LOG", p), \
             patch("storage.get_all_users", side_effect=_users), \
             patch("storage.get_appointments", side_effect=_appts), \
             patch("storage.get_announcements", side_effect=_anns):
            return _run(st.build_usage_report(days, clamped, now))

    def test_user_counts_and_joins(self, tmp_path):
        now = datetime.now(TZ)
        lines = [_log_line(now - timedelta(days=1), "USER_JOINED", "User started the bot"),
                 _log_line(now - timedelta(days=2), "USER_LEFT", "User stopped the bot")]
        text = self._report(tmp_path, lines,
                            users=[{"chat_id": 1, "language": "en"},
                                   {"chat_id": 2, "language": "es"}], now=now)
        assert "*Registered users:* 2" in text
        assert "+1 new" in text and "-1 left" in text
        assert "en 1" in text and "es 1" in text

    def test_reminder_opt_ins_counted(self, tmp_path):
        users = [{"chat_id": 1, "notif_prefs": ["convocations", "special"]},
                 {"chat_id": 2, "notif_prefs": ["convocations"]}]
        text = self._report(tmp_path, [], users=users)
        assert "convocations 2" in text
        assert "special 1" in text

    def test_no_opt_ins_reported_cleanly(self, tmp_path):
        text = self._report(tmp_path, [], users=[{"chat_id": 1}])
        assert "opt-ins:* none" in text

    def test_only_upcoming_appointments_are_counted(self, tmp_path):
        now = datetime.now(TZ)
        soon = (now + timedelta(days=3)).isoformat()
        far = (now + timedelta(days=60)).isoformat()
        past = (now - timedelta(days=5)).isoformat()
        appts = [{"status": "confirmed", "confirmed_datetime": soon},
                 {"status": "pending", "requested_datetime": far},
                 {"status": "cancelled", "requested_datetime": soon},
                 {"status": "confirmed", "confirmed_datetime": past}]   # already happened
        text = self._report(tmp_path, [], appts=appts, now=now)
        assert "*Upcoming appointments:* 3" in text
        assert "within next 30 days: 2" in text

    def test_past_appointments_excluded_from_status_breakdown(self, tmp_path):
        now = datetime.now(TZ)
        future = (now + timedelta(days=2)).isoformat()
        past = (now - timedelta(days=9)).isoformat()
        appts = [{"status": "confirmed", "confirmed_datetime": future},
                 {"status": "declined", "requested_datetime": past}]
        text = self._report(tmp_path, [], appts=appts, now=now)
        assert "confirmed: 1" in text
        assert "declined" not in text, "a past appointment must not appear in the breakdown"

    def test_all_past_reports_zero(self, tmp_path):
        now = datetime.now(TZ)
        appts = [{"status": "confirmed",
                  "confirmed_datetime": (now - timedelta(days=k)).isoformat()}
                 for k in (1, 20, 200)]
        text = self._report(tmp_path, [], appts=appts, now=now)
        assert "*Upcoming appointments:* 0" in text

    def test_unparseable_appointment_date_flagged_not_counted(self, tmp_path):
        appts = [{"status": "pending", "requested_datetime": "not-a-date"}]
        text = self._report(tmp_path, [], appts=appts)
        assert "*Upcoming appointments:* 0" in text
        assert "unreadable date" in text

    def test_notification_recipients_totalled(self, tmp_path):
        now = datetime.now(TZ)
        lines = [_log_line(now, "NOTIFICATION", "Sent notification for 'A' to 3 user(s)"),
                 _log_line(now, "NOTIFICATION", "Sent notification for 'B' to 5 user(s)")]
        text = self._report(tmp_path, lines, now=now)
        assert "2 broadcast(s), 8 recipient(s)" in text

    def test_top_commands_listed(self, tmp_path):
        now = datetime.now(TZ)
        lines = ([_log_line(now, "COMMAND", "/events")] * 3
                 + [_log_line(now, "COMMAND", "/help — topic=x")] * 2)
        text = self._report(tmp_path, lines, now=now)
        assert "*Commands used:* 5" in text
        assert "/events — 3" in text
        assert "/help — 2" in text

    def test_active_announcements_counted(self, tmp_path):
        now = datetime.now(TZ)
        anns = [{"id": "A", "expires": (now + timedelta(days=2)).strftime("%Y-%m-%d"),
                 "created": now.isoformat()},
                {"id": "B", "expires": (now - timedelta(days=2)).strftime("%Y-%m-%d"),
                 "created": now.isoformat()}]
        text = self._report(tmp_path, [], anns=anns, now=now)
        assert "*Active announcements:* 1" in text

    def test_markdown_in_command_names_escaped(self, tmp_path):
        now = datetime.now(TZ)
        lines = [_log_line(now, "COMMAND", "/contact_share")]
        text = self._report(tmp_path, lines, now=now)
        assert "/contact\\_share" in text


# ---------------------------------------------------------------------------
# Command wiring
# ---------------------------------------------------------------------------

class TestCmdStats:
    def _run_cmd(self, args):
        ctx = MagicMock()
        ctx.args = args
        upd = _upd()
        with patch("permissions.is_admin", return_value=True), \
             patch.object(st, "build_system_report", return_value="SYSTEM-VIEW") as sysrep, \
             patch.object(st, "build_usage_report",
                          new=AsyncMock(return_value="USAGE-VIEW")) as usagerep:
            _run(st.cmd_stats(upd, ctx))
        return upd.message.reply_text.call_args[0][0], sysrep, usagerep

    def test_default_shows_system_view(self):
        text, sysrep, usagerep = self._run_cmd([])
        assert text == "SYSTEM-VIEW"
        assert sysrep.call_args[0][0] == 7
        usagerep.assert_not_awaited()

    def test_usage_argument_shows_usage_view(self):
        text, sysrep, usagerep = self._run_cmd(["usage"])
        assert text == "USAGE-VIEW"
        sysrep.assert_not_called()

    def test_period_passed_through(self):
        _, sysrep, _ = self._run_cmd(["21"])
        assert sysrep.call_args[0][0] == 21

    def test_clamped_flag_passed_through(self):
        _, sysrep, _ = self._run_cmd(["90"])
        assert sysrep.call_args[0][0] == 30
        assert sysrep.call_args[0][1] is True

    def test_non_admin_blocked(self):
        ctx = MagicMock()
        ctx.args = []
        upd = _upd()
        with patch("permissions.is_admin", return_value=False):
            _run(st.cmd_stats(upd, ctx))
        assert "Unknown command" in upd.message.reply_text.call_args[0][0]


# ---------------------------------------------------------------------------
# Scheduler / notification-engine health
# ---------------------------------------------------------------------------

class _Job:
    """Stand-in for telegram.ext.Job (only .name and .next_t are read)."""

    def __init__(self, name, next_t=None):
        self.name = name
        self.next_t = next_t


class _Queue:
    def __init__(self, jobs, raises=False):
        self._jobs = jobs
        self._raises = raises

    def jobs(self):
        if self._raises:
            raise RuntimeError("scheduler unavailable")
        return self._jobs


class TestSchedulerSnapshot:
    def test_none_without_a_queue(self):
        assert st.scheduler_snapshot(None) is None

    def test_counts_event_reminder_jobs(self):
        now = datetime.now(TZ)
        q = _Queue([_Job("notif_a", now + timedelta(hours=2)),
                    _Job("notif_b", now + timedelta(hours=9))])
        snap = st.scheduler_snapshot(q, now)
        assert snap["event_jobs"] == 2

    def test_reports_the_soonest_reminder(self):
        now = datetime.now(TZ)
        q = _Queue([_Job("notif_later", now + timedelta(hours=9)),
                    _Job("notif_soon", now + timedelta(hours=2))])
        snap = st.scheduler_snapshot(q, now)
        assert snap["next_event_name"] == "soon"
        assert snap["next_event_at"] == now + timedelta(hours=2)

    def test_recurring_jobs_listed_separately(self):
        now = datetime.now(TZ)
        q = _Queue([_Job("notif_a", now + timedelta(hours=1)),
                    _Job("nightly_backup", now + timedelta(hours=10)),
                    _Job("daily_maintenance", now + timedelta(hours=20))])
        snap = st.scheduler_snapshot(q, now)
        assert snap["event_jobs"] == 1
        assert set(snap["recurring"]) == {"nightly_backup", "daily_maintenance"}

    def test_empty_queue_reports_zero(self):
        snap = st.scheduler_snapshot(_Queue([]), datetime.now(TZ))
        assert snap["event_jobs"] == 0
        assert snap["next_event_at"] is None

    def test_jobs_without_a_next_time_are_not_offered_as_next(self):
        """A removed/exhausted job has next_t None and must not be picked."""
        now = datetime.now(TZ)
        q = _Queue([_Job("notif_dead", None), _Job("notif_live", now + timedelta(hours=3))])
        snap = st.scheduler_snapshot(q, now)
        assert snap["next_event_name"] == "live"

    def test_unreadable_queue_does_not_break_stats(self):
        assert st.scheduler_snapshot(_Queue([], raises=True)) is None


class TestLastEntryOf:
    def test_finds_most_recent_of_the_kind(self, tmp_path):
        now = datetime.now(TZ)
        p = _write_log(tmp_path, [
            _log_line(now - timedelta(days=9), "NOTIFICATION",
                      "Sent notification for 'A' to 2 user(s)"),
            _log_line(now - timedelta(days=2), "NOTIFICATION",
                      "Sent notification for 'B' to 5 user(s)"),
            _log_line(now, "COMMAND", "/events"),
        ])
        with patch.object(st, "ACTIVITY_LOG", p):
            found = st.last_entry_of("NOTIFICATION")
        assert "'B'" in found["detail"]

    def test_looks_beyond_the_report_period(self, tmp_path):
        """'No reminder in 7 days' is ambiguous; '30 days ago' is actionable."""
        now = datetime.now(TZ)
        p = _write_log(tmp_path, [
            _log_line(now - timedelta(days=30), "NOTIFICATION",
                      "Sent notification for 'Old' to 1 user(s)"),
        ])
        with patch.object(st, "ACTIVITY_LOG", p):
            found = st.last_entry_of("NOTIFICATION")
        assert found is not None and "Old" in found["detail"]

    def test_none_when_kind_never_occurred(self, tmp_path):
        now = datetime.now(TZ)
        p = _write_log(tmp_path, [_log_line(now, "COMMAND", "/events")])
        with patch.object(st, "ACTIVITY_LOG", p):
            assert st.last_entry_of("NOTIFICATION") is None

    def test_missing_log_is_tolerated(self, tmp_path):
        with patch.object(st, "ACTIVITY_LOG", tmp_path / "absent.log"):
            assert st.last_entry_of("NOTIFICATION") is None


class TestSystemReportScheduler:
    def _report(self, tmp_path, lines=(), queue=None, now=None):
        now = now or datetime.now(TZ)
        p = _write_log(tmp_path, list(lines))
        with patch.object(st, "ACTIVITY_LOG", p):
            return st.build_system_report(7, False, now, job_queue=queue)

    def test_shows_scheduled_count_and_next(self, tmp_path):
        now = datetime.now(TZ)
        q = _Queue([_Job("notif_sabbath_eve", now + timedelta(hours=4))])
        text = self._report(tmp_path, queue=q, now=now)
        assert "*Event reminders scheduled:* 1" in text
        assert "sabbath\\_eve" in text          # escaped for Markdown
        assert "in 4h" in text

    def test_warns_when_nothing_is_queued(self, tmp_path):
        """The failure this whole section exists to surface."""
        text = self._report(tmp_path, queue=_Queue([]))
        assert "none queued" in text

    def test_lists_recurring_jobs(self, tmp_path):
        now = datetime.now(TZ)
        q = _Queue([_Job("nightly_backup", now + timedelta(hours=6))])
        text = self._report(tmp_path, queue=q, now=now)
        assert "nightly\\_backup" in text

    def test_reports_last_delivered_reminder(self, tmp_path):
        now = datetime.now(TZ)
        lines = [_log_line(now - timedelta(days=3), "NOTIFICATION",
                           "Sent notification for 'Sabbath' to 4 user(s)")]
        text = self._report(tmp_path, lines, queue=_Queue([]), now=now)
        assert "*Last reminder delivered:*" in text
        assert "3d" in text

    def test_reports_when_none_on_record(self, tmp_path):
        text = self._report(tmp_path, queue=_Queue([]))
        assert "none on record" in text

    def test_section_degrades_without_a_queue(self, tmp_path):
        text = self._report(tmp_path, queue=None)
        assert "_unavailable_" in text

    def test_report_still_renders_when_queue_errors(self, tmp_path):
        text = self._report(tmp_path, queue=_Queue([], raises=True))
        assert "*Uptime:*" in text        # the rest of the report survives


class TestCmdStatsPassesJobQueue:
    def test_job_queue_reaches_the_report(self):
        ctx = MagicMock()
        ctx.args = []
        sentinel = _Queue([])
        ctx.application.job_queue = sentinel
        upd = _upd()
        with patch("permissions.is_admin", return_value=True), \
             patch.object(st, "build_system_report", return_value="OK") as report:
            _run(st.cmd_stats(upd, ctx))
        assert report.call_args.kwargs.get("job_queue") is sentinel

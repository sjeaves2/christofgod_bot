"""Tests for unhandled-exception capture and ops-admin alerting.

Covers the two noise controls that make this safe to run unattended (transient
network hold, fingerprint de-duplication), the durable error log, and the
plain-text alert format.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import error_reporting as er
import permissions


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _raise(kind=ValueError, msg="something broke"):
    """Return a raised-and-caught exception, so it carries a real traceback."""
    try:
        raise kind(msg)
    except BaseException as exc:  # noqa: BLE001 - deliberate capture
        return exc


def _reset():
    er._alert_state.clear()
    er._recent.clear()


class TestClassification:
    def test_network_errors_are_transient(self):
        from telegram.error import NetworkError, TimedOut
        assert er.is_transient(_raise(NetworkError, "Bad Gateway"))
        assert er.is_transient(_raise(TimedOut, "timed out"))

    def test_message_markers_are_transient(self):
        assert er.is_transient(_raise(RuntimeError, "Service Unavailable"))

    def test_bad_request_is_not_transient(self):
        """BadRequest subclasses NetworkError in PTB, but it's a real bug —
        this is the 'can't parse entities' failure class."""
        from telegram.error import BadRequest
        assert er.is_transient(_raise(BadRequest, "can't parse entities")) is False

    def test_conflict_and_forbidden_alert_immediately(self):
        from telegram.error import Conflict, Forbidden
        assert er.is_transient(_raise(Conflict, "other getUpdates")) is False
        assert er.is_transient(_raise(Forbidden, "bot was blocked")) is False

    def test_ordinary_bug_is_not_transient(self):
        assert er.is_transient(_raise(ValueError, "real bug")) is False


class TestFingerprint:
    def test_includes_type_and_location(self):
        fp = er.fingerprint(_raise(ValueError, "x"))
        assert fp.startswith("ValueError@")
        assert ".py:" in fp

    def test_different_types_differ(self):
        assert er.fingerprint(_raise(ValueError, "x")) != er.fingerprint(_raise(KeyError, "x"))

    def test_same_site_same_fingerprint(self):
        assert er.fingerprint(_raise(ValueError, "a")) == er.fingerprint(_raise(ValueError, "b"))


class TestTransientHold:
    def test_single_blip_is_silent(self):
        _reset()
        from telegram.error import NetworkError
        send, _ = er.should_alert(_raise(NetworkError, "Bad Gateway"))
        assert send is False

    def test_alerts_once_threshold_reached(self):
        _reset()
        from telegram.error import NetworkError
        results = [er.should_alert(_raise(NetworkError, "Bad Gateway"))[0]
                   for _ in range(er.TRANSIENT_THRESHOLD)]
        assert results[:-1] == [False] * (er.TRANSIENT_THRESHOLD - 1)
        assert results[-1] is True

    def test_occurrences_outside_window_do_not_accumulate(self):
        _reset()
        from telegram.error import NetworkError
        now = er._now()
        old = now - er.TRANSIENT_WINDOW - timedelta(minutes=1)
        for _ in range(er.TRANSIENT_THRESHOLD - 1):
            er.should_alert(_raise(NetworkError, "Bad Gateway"), now=old)
        send, _ = er.should_alert(_raise(NetworkError, "Bad Gateway"), now=now)
        assert send is False, "stale occurrences must not count toward the threshold"

    def test_real_bug_alerts_on_first_occurrence(self):
        _reset()
        send, _ = er.should_alert(_raise(ValueError, "real bug"))
        assert send is True


class TestDeduplication:
    def test_repeats_suppressed_and_counted(self):
        _reset()
        assert er.should_alert(_raise(ValueError, "boom"))[0] is True
        for _ in range(13):
            er.should_alert(_raise(ValueError, "boom"))
        send, suppressed = er.should_alert(_raise(ValueError, "boom"))
        assert send is False
        assert suppressed == 14

    def test_alert_resumes_after_cooldown(self):
        _reset()
        now = er._now()
        er.should_alert(_raise(ValueError, "boom"), now=now)
        later = now + er.ALERT_COOLDOWN + timedelta(minutes=1)
        send, _ = er.should_alert(_raise(ValueError, "boom"), now=later)
        assert send is True

    def test_suppressed_count_rolls_into_next_alert(self):
        _reset()
        now = er._now()
        er.should_alert(_raise(ValueError, "boom"), now=now)
        for _ in range(4):
            er.should_alert(_raise(ValueError, "boom"), now=now)
        later = now + er.ALERT_COOLDOWN + timedelta(minutes=1)
        send, suppressed = er.should_alert(_raise(ValueError, "boom"), now=later)
        assert send is True and suppressed == 4

    def test_distinct_errors_alert_independently(self):
        _reset()
        assert er.should_alert(_raise(ValueError, "a"))[0] is True
        assert er.should_alert(_raise(KeyError, "b"))[0] is True


class TestAlertText:
    def test_includes_id_type_and_message(self):
        text = er.build_alert("ERR-ABC123", _raise(ValueError, "bad thing"))
        assert "ERR-ABC123" in text
        assert "ValueError: bad thing" in text

    def test_includes_line_numbers(self):
        text = er.build_alert("ERR-ABC123", _raise(ValueError, "x"))
        assert "test_error_reporting.py:" in text, "must show file:line for tracking"

    def test_includes_context_note(self):
        text = er.build_alert("ERR-1", _raise(ValueError, "x"), note="user 5, input '/events'")
        assert "user 5" in text

    def test_reports_suppressed_count(self):
        text = er.build_alert("ERR-1", _raise(ValueError, "x"), suppressed=9)
        assert "+9 more" in text

    def test_points_at_the_durable_log(self):
        text = er.build_alert("ERR-XYZ", _raise(ValueError, "x"))
        assert "errors.log" in text and "ERR-XYZ" in text

    def test_truncated_below_telegram_limit(self):
        long_exc = _raise(ValueError, "y" * 8000)
        text = er.build_alert("ERR-1", long_exc)
        assert len(text) <= er.MAX_DM_CHARS + 60
        assert "truncated" in text


class TestErrorLog:
    def test_writes_full_trace(self, tmp_path):
        with patch.object(er, "ERRORS_LOG", tmp_path / "errors.log"):
            er.write_error_log("ERR-AAA111", _raise(ValueError, "kaboom"), note="ctx")
            written = (tmp_path / "errors.log").read_text()
        assert "ERR-AAA111" in written
        assert "type   : ValueError" in written
        assert "kaboom" in written
        assert "Traceback (most recent call last)" in written
        assert "ctx" in written

    def test_appends_rather_than_overwrites(self, tmp_path):
        with patch.object(er, "ERRORS_LOG", tmp_path / "errors.log"):
            er.write_error_log("ERR-1", _raise(ValueError, "first"))
            er.write_error_log("ERR-2", _raise(KeyError, "second"))
            written = (tmp_path / "errors.log").read_text()
        assert "ERR-1" in written and "ERR-2" in written

    def test_write_failure_does_not_raise(self, tmp_path):
        # Directory where a file is expected -> OSError inside the writer.
        bad = tmp_path / "errors.log"
        bad.mkdir()
        with patch.object(er, "ERRORS_LOG", bad):
            er.write_error_log("ERR-1", _raise(ValueError, "x"))   # must not raise


class TestOpsRecipients:
    def test_ops_admin_by_username(self):
        async def _users():
            return [{"chat_id": 1, "username": "bishop"}, {"chat_id": 2, "username": "other"}]
        with patch("storage.get_all_users", side_effect=_users), \
             patch.object(permissions, "OPS_USERNAMES", {"bishop"}), \
             patch.object(permissions, "_ops_chat_ids", set()):
            assert _run(er.ops_chat_ids()) == {1}

    def test_ops_admin_registered_by_phone(self):
        async def _users():
            return []
        with patch("storage.get_all_users", side_effect=_users), \
             patch.object(permissions, "OPS_USERNAMES", set()), \
             patch.object(permissions, "_ops_chat_ids", {99}):
            assert _run(er.ops_chat_ids()) == {99}

    def test_non_ops_admins_excluded(self):
        async def _users():
            return [{"chat_id": 5, "username": "plainadmin"}]
        with patch("storage.get_all_users", side_effect=_users), \
             patch.object(permissions, "OPS_USERNAMES", {"bishop"}), \
             patch.object(permissions, "_ops_chat_ids", set()):
            assert _run(er.ops_chat_ids()) == set()


class TestReportException:
    def _report(self, exc, tmp_path, users=None, ops={"bishop"}):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        async def _users():
            return users if users is not None else [{"chat_id": 7, "username": "bishop"}]

        with patch.object(er, "ERRORS_LOG", tmp_path / "errors.log"), \
             patch("storage.get_all_users", side_effect=_users), \
             patch.object(permissions, "OPS_USERNAMES", ops), \
             patch.object(permissions, "_ops_chat_ids", set()):
            error_id = _run(er.report_exception(bot, exc, note="user 7"))
        return error_id, bot, (tmp_path / "errors.log")

    def test_alerts_ops_admin_in_plain_text(self, tmp_path):
        _reset()
        error_id, bot, _ = self._report(_raise(ValueError, "boom"), tmp_path)
        assert error_id.startswith("ERR-")
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args[0][0] == 7
        # Plain text: a traceback is not valid Markdown.
        assert "parse_mode" not in bot.send_message.await_args.kwargs

    def test_logs_even_when_alert_suppressed(self, tmp_path):
        _reset()
        from telegram.error import NetworkError
        error_id, bot, log = self._report(_raise(NetworkError, "Bad Gateway"), tmp_path)
        bot.send_message.assert_not_awaited()          # single blip stays quiet
        assert error_id in log.read_text()             # but is still recorded

    def test_survives_no_reachable_ops_admin(self, tmp_path):
        _reset()
        error_id, bot, log = self._report(_raise(ValueError, "boom"), tmp_path, users=[])
        bot.send_message.assert_not_awaited()
        assert error_id in log.read_text()

    def test_send_failure_does_not_raise(self, tmp_path):
        _reset()
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))

        async def _users():
            return [{"chat_id": 7, "username": "bishop"}]

        with patch.object(er, "ERRORS_LOG", tmp_path / "errors.log"), \
             patch("storage.get_all_users", side_effect=_users), \
             patch.object(permissions, "OPS_USERNAMES", {"bishop"}), \
             patch.object(permissions, "_ops_chat_ids", set()):
            _run(er.report_exception(bot, _raise(ValueError, "boom")))  # must not raise

    def test_none_exception_is_ignored(self, tmp_path):
        assert _run(er.report_exception(MagicMock(), None)) is None

    def test_activity_log_records_error_id(self, tmp_path):
        _reset()
        with patch.object(er.activity, "log_error") as log_error:
            self._report(_raise(ValueError, "boom"), tmp_path)
        assert log_error.called
        assert "ERR-" in log_error.call_args[0][0]
        assert "ValueError" in log_error.call_args[0][0]


class TestResetErrorLog:
    """A restart normally means new code, so last run's traces are stale."""

    def test_clears_existing_log(self, tmp_path):
        log = tmp_path / "errors.log"
        log.write_text("===== ERR-OLD1 =====\nstale trace\n", encoding="utf-8")
        with patch.object(er, "ERRORS_LOG", log):
            discarded = er.reset_error_log()
        assert discarded > 0
        assert log.read_text() == ""

    def test_missing_log_is_noop(self, tmp_path):
        with patch.object(er, "ERRORS_LOG", tmp_path / "absent.log"):
            assert er.reset_error_log() == 0

    def test_clears_in_memory_alert_state(self, tmp_path):
        _reset()
        er.should_alert(_raise(ValueError, "boom"))           # arms the cooldown
        assert er._alert_state
        with patch.object(er, "ERRORS_LOG", tmp_path / "errors.log"):
            er.reset_error_log()
        assert er._alert_state == {} and er._recent == {}

    def test_alerting_resumes_after_reset(self, tmp_path):
        """Post-restart, the first occurrence must alert again rather than be
        suppressed by state from the previous run."""
        _reset()
        assert er.should_alert(_raise(ValueError, "boom"))[0] is True
        assert er.should_alert(_raise(ValueError, "boom"))[0] is False
        with patch.object(er, "ERRORS_LOG", tmp_path / "errors.log"):
            er.reset_error_log()
        assert er.should_alert(_raise(ValueError, "boom"))[0] is True

    def test_new_errors_still_write_after_reset(self, tmp_path):
        log = tmp_path / "errors.log"
        log.write_text("old\n", encoding="utf-8")
        with patch.object(er, "ERRORS_LOG", log):
            er.reset_error_log()
            er.write_error_log("ERR-NEW1", _raise(ValueError, "fresh"))
        text = log.read_text()
        assert "ERR-NEW1" in text and "old" not in text

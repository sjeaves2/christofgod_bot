"""Tests for unhandled-exception capture and ops-admin alerting.

Covers the two noise controls that make this safe to run unattended (transient
network hold, fingerprint de-duplication), the durable error log, and the
plain-text alert format.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import error_reporting as er
import permissions


def _raise(kind=ValueError, msg="something broke"):
    """Return a raised-and-caught exception, so it carries a real traceback."""
    try:
        raise kind(msg)
    except BaseException as exc:  # noqa: BLE001 - deliberate capture
        return exc


def _reset():
    er._alert_state.clear()
    er._episodes.clear()


class TestTheSuiteCannotTouchTheRealLogs:
    """conftest redirects logs/ into tmp_path. This checks it covers EVERY
    file, not just the ones someone remembered.

    PENDING_ALERTS was missed for three releases: the queue was added in
    v0.24.1 and conftest was written before it existed. Nothing failed,
    because logs/ is gitignored, so fake ops alerts accumulated in the real
    logs/pending_alerts.json where git status would never show them.

    Derived from the module rather than listed here on purpose. A list would
    need the same update conftest needs, and would be forgotten in the same
    breath.
    """

    def _log_file_constants(self):
        """Module-level Path constants naming a FILE (not the directory)."""
        return {name: value for name, value in vars(er).items()
                if name.isupper() and isinstance(value, Path) and value.suffix}

    def test_the_scan_finds_the_constants_it_is_meant_to_check(self):
        """Guards against the regex-typo failure mode: a scan that sees
        nothing passes vacuously forever."""
        found = self._log_file_constants()
        assert {"ERRORS_LOG", "PENDING_ALERTS"} <= set(found), found

    def test_no_log_file_constant_points_into_the_repository(self):
        repo_logs = (Path(__file__).resolve().parents[1] / "logs").resolve()
        for name, value in self._log_file_constants().items():
            resolved = Path(value).resolve()
            assert repo_logs not in resolved.parents, (
                f"error_reporting.{name} points at {resolved}, inside the real "
                f"logs/ directory. Add it to the _isolate_logs fixture in "
                f"tests/conftest.py — a test run must never write there.")


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

    def test_alerts_once_the_condition_has_persisted(self):
        _reset()
        from telegram.error import NetworkError
        now = er._now()
        assert er.should_alert(_raise(NetworkError, "Bad Gateway"), now=now)[0] is False
        # Kept alive at half EPISODE_GAP so it stays ONE episode. A fraction,
        # never a subtraction: a step computed as EPISODE_GAP minus a constant
        # becomes zero if EPISODE_GAP is ever shortened past it, and this loop
        # would then spin forever instead of failing — which is exactly what a
        # mutation to EPISODE_GAP made it do.
        step = er.EPISODE_GAP / 2
        assert step > timedelta(0), "step must advance or this loop cannot end"
        t = now
        send = False
        for _ in range(1000):
            if t - now >= er.SUSTAINED_FOR:
                break
            t += step
            send, _ = er.should_alert(_raise(NetworkError, "Bad Gateway"), now=t)
        else:
            raise AssertionError(
                f"never reached {er.SUSTAINED_FOR} stepping {step}")
        assert send is True, (
            f"an episode lasting {t - now} should alert at {er.SUSTAINED_FOR}")

    def test_a_dense_short_burst_never_alerts(self):
        """The real 2026-11-02 cluster: six Bad Gateways in fourteen seconds.
        Under the old count rule this alerted; it is a harmless scheduled blip
        at Telegram and the bot recovered on its own."""
        _reset()
        from telegram.error import NetworkError
        now = er._now()
        for offset in (0, 1, 3, 5, 9, 14):
            send, _ = er.should_alert(_raise(NetworkError, "Bad Gateway"),
                                      now=now + timedelta(seconds=offset))
            assert send is False, f"alerted after only {offset}s of trouble"

    def test_a_long_quiet_gap_starts_a_new_episode(self):
        """Otherwise two unrelated blips an hour apart weld into one 'episode'
        that alerts about a problem which is not happening."""
        _reset()
        from telegram.error import NetworkError
        now = er._now()
        er.should_alert(_raise(NetworkError, "Bad Gateway"), now=now)
        later = now + er.EPISODE_GAP + timedelta(minutes=57)
        send, _ = er.should_alert(_raise(NetworkError, "Bad Gateway"), now=later)
        assert send is False
        assert er.episode_duration(_raise(NetworkError, "Bad Gateway"),
                                   later) < er.SUSTAINED_FOR

    def test_occurrence_count_alone_cannot_trigger_an_alert(self):
        """The property that replaced the threshold: no number of errors in
        the same instant is enough, because none of them is duration."""
        _reset()
        from telegram.error import NetworkError
        now = er._now()
        for _ in range(200):
            send, _ = er.should_alert(_raise(NetworkError, "Bad Gateway"), now=now)
            assert send is False

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
    async def test_ops_admin_by_username(self):
        async def _users():
            return [{"chat_id": 1, "username": "bishop"}, {"chat_id": 2, "username": "other"}]
        with patch("storage.get_all_users", side_effect=_users), \
             patch.object(permissions, "OPS_USERNAMES", {"bishop"}), \
             patch.object(permissions, "_ops_chat_ids", set()):
            assert await er.ops_chat_ids() == {1}

    async def test_ops_admin_registered_by_phone(self):
        async def _users():
            return []
        with patch("storage.get_all_users", side_effect=_users), \
             patch.object(permissions, "OPS_USERNAMES", set()), \
             patch.object(permissions, "_ops_chat_ids", {99}):
            assert await er.ops_chat_ids() == {99}

    async def test_non_ops_admins_excluded(self):
        async def _users():
            return [{"chat_id": 5, "username": "plainadmin"}]
        with patch("storage.get_all_users", side_effect=_users), \
             patch.object(permissions, "OPS_USERNAMES", {"bishop"}), \
             patch.object(permissions, "_ops_chat_ids", set()):
            assert await er.ops_chat_ids() == set()


class TestOpsRecipientsByStoredPhone:
    """The stored-phone route, and the restart that used to erase it.

    A `phone:` in admins.yaml is the backstop for an ops admin who changes
    their Telegram username. It was not actually one: the number was compared
    only against a live contact-share, so the match lived in the in-memory
    permissions._ops_chat_ids and was gone at the next restart. Every test
    here therefore patches `_ops_chat_ids` to an EMPTY set — that empty set IS
    the restart, and before this change every assertion below returned
    set().
    """

    @staticmethod
    def _ctx(users, usernames=frozenset(), phones=frozenset()):
        async def _users():
            return users
        return (patch("storage.get_all_users", side_effect=_users),
                patch.object(permissions, "OPS_USERNAMES", set(usernames)),
                patch.object(permissions, "OPS_PHONES", set(phones)),
                patch.object(permissions, "_ops_chat_ids", set()))

    async def _ids(self, users, usernames=frozenset(), phones=frozenset()):
        a, b, c, d = self._ctx(users, usernames, phones)
        with a, b, c, d:
            return await er.ops_chat_ids()

    async def test_stored_phone_is_matched_after_a_restart(self):
        """The whole point: no live contact-share, no username match."""
        users = [{"chat_id": 7, "username": "renamed", "phone": "15550001234"}]
        assert await self._ids(users, phones={"15550001234"}) == {7}

    async def test_stored_phone_is_normalised_before_comparison(self):
        """users.yaml may hold punctuation; OPS_PHONES is digits-only."""
        users = [{"chat_id": 7, "phone": "+1 (555) 000-1234"}]
        assert await self._ids(users, phones={"15550001234"}) == {7}

    async def test_a_phone_that_is_not_an_ops_phone_is_ignored(self):
        users = [{"chat_id": 7, "phone": "15550009999"}]
        assert await self._ids(users, phones={"15550001234"}) == set()

    async def test_a_user_with_neither_is_ignored(self):
        users = [{"chat_id": 7, "username": "nobody"}]
        assert await self._ids(users, usernames={"bishop"},
                               phones={"15550001234"}) == set()

    async def test_a_matching_phone_without_a_chat_id_is_skipped(self):
        """Unreachable is not reachable — a chat_id is how we send anything."""
        users = [{"chat_id": None, "phone": "15550001234"},
                 {"phone": "15550001234"}]
        assert await self._ids(users, phones={"15550001234"}) == set()

    async def test_username_and_phone_on_one_person_yields_one_id(self):
        """Both routes hitting the same person must not double-count."""
        users = [{"chat_id": 7, "username": "bishop", "phone": "15550001234"}]
        assert await self._ids(users, usernames={"bishop"},
                               phones={"15550001234"}) == {7}

    async def test_both_routes_together_reach_both_people(self):
        users = [{"chat_id": 1, "username": "bishop"},
                 {"chat_id": 2, "username": "renamed", "phone": "15550001234"}]
        assert await self._ids(users, usernames={"bishop"},
                               phones={"15550001234"}) == {1, 2}

    async def test_an_empty_ops_phones_set_matches_nobody(self):
        """Guards against a falsy-phone bug matching every user."""
        users = [{"chat_id": 1, "phone": ""}, {"chat_id": 2},
                 {"chat_id": 3, "phone": None}]
        assert await self._ids(users, phones=set()) == set()

    async def test_the_phone_route_is_what_makes_this_pass(self):
        """Paired guard: the same input with the phone route's input removed.

        Without this, a bug that returned every chat_id unconditionally would
        satisfy every assertion above.
        """
        users = [{"chat_id": 7, "username": "renamed", "phone": "15550001234"}]
        assert await self._ids(users, phones={"15550001234"}) == {7}
        assert await self._ids(users, phones=set()) == set()


class TestReportException:
    async def _report(self, exc, tmp_path, users=None, ops={"bishop"}):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        async def _users():
            return users if users is not None else [{"chat_id": 7, "username": "bishop"}]

        with patch.object(er, "ERRORS_LOG", tmp_path / "errors.log"), \
             patch("storage.get_all_users", side_effect=_users), \
             patch.object(permissions, "OPS_USERNAMES", ops), \
             patch.object(permissions, "_ops_chat_ids", set()):
            error_id = await er.report_exception(bot, exc, note="user 7")
        return error_id, bot, (tmp_path / "errors.log")

    async def test_alerts_ops_admin_in_plain_text(self, tmp_path):
        _reset()
        error_id, bot, _ = await self._report(_raise(ValueError, "boom"), tmp_path)
        assert error_id.startswith("ERR-")
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args[0][0] == 7
        # Plain text: a traceback is not valid Markdown.
        assert "parse_mode" not in bot.send_message.await_args.kwargs

    async def test_logs_even_when_alert_suppressed(self, tmp_path):
        _reset()
        from telegram.error import NetworkError
        error_id, bot, log = await self._report(_raise(NetworkError, "Bad Gateway"), tmp_path)
        bot.send_message.assert_not_awaited()          # single blip stays quiet
        assert error_id in log.read_text()             # but is still recorded

    async def test_survives_no_reachable_ops_admin(self, tmp_path):
        _reset()
        error_id, bot, log = await self._report(_raise(ValueError, "boom"), tmp_path, users=[])
        bot.send_message.assert_not_awaited()
        assert error_id in log.read_text()

    async def test_send_failure_does_not_raise(self, tmp_path):
        _reset()
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))

        async def _users():
            return [{"chat_id": 7, "username": "bishop"}]

        with patch.object(er, "ERRORS_LOG", tmp_path / "errors.log"), \
             patch("storage.get_all_users", side_effect=_users), \
             patch.object(permissions, "OPS_USERNAMES", {"bishop"}), \
             patch.object(permissions, "_ops_chat_ids", set()):
            await er.report_exception(bot, _raise(ValueError, "boom"))  # must not raise

    async def test_none_exception_is_ignored(self, tmp_path):
        assert await er.report_exception(MagicMock(), None) is None

    async def test_activity_log_records_error_id(self, tmp_path):
        _reset()
        with patch.object(er.activity, "log_error") as log_error:
            await self._report(_raise(ValueError, "boom"), tmp_path)
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
        assert er._alert_state == {} and er._episodes == {}

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


class _Pending:
    """Redirect the held-alert file into tmp_path for the duration of a test."""

    def __init__(self, tmp_path):
        self.path = tmp_path / "pending_alerts.json"

    def __enter__(self):
        self._patch = patch.object(er, "PENDING_ALERTS", self.path)
        self._patch.start()
        return self.path

    def __exit__(self, *exc):
        self._patch.stop()


def _ops_bot(fail=False):
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=RuntimeError("Bad Gateway") if fail else None)
    return bot


def _as_ops(users=None):
    """Patch context so chat_id 7 is the one reachable ops admin."""
    async def _users():
        return users if users is not None else [{"chat_id": 7, "username": "bishop"}]
    return (patch("storage.get_all_users", side_effect=_users),
            patch.object(permissions, "OPS_USERNAMES", {"bishop"}),
            patch.object(permissions, "_ops_chat_ids", set()))


def _hold(path, error_id="ERR-HELD1", text="⚠️ Bot error ERR-HELD1\nValueError: boom",
          age=timedelta(0)):
    """Write one held alert straight to disk, aged by *age*."""
    import json
    queued_at = (er._now() - age).isoformat()
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    existing.append({"error_id": error_id, "queued_at": queued_at, "text": text})
    path.write_text(json.dumps(existing), encoding="utf-8")


class TestHoldingUndeliverableAlerts:
    """The 2026-10-27 failure: the alert about the network went out over the
    network that was broken, the send raised, and report_exception logged a
    warning and dropped it. It must be held instead.
    """

    async def _report(self, tmp_path, exc, fail):
        bot = _ops_bot(fail=fail)
        users, ops_names, ops_ids = _as_ops()
        with _Pending(tmp_path) as pending, \
             patch.object(er, "ERRORS_LOG", tmp_path / "errors.log"), \
             users, ops_names, ops_ids:
            error_id = await er.report_exception(bot, exc, note="user 7")
            held = er._load_pending()
        return error_id, bot, pending, held

    async def test_a_failed_send_is_held(self, tmp_path):
        _reset()
        error_id, bot, pending, held = await self._report(
            tmp_path, _raise(ValueError, "boom"), fail=True)
        bot.send_message.assert_awaited()            # it really tried
        assert pending.exists(), "the alert reached nobody and was not held"
        assert [e["error_id"] for e in held] == [error_id]
        assert "boom" in held[0]["text"]

    async def test_a_delivered_alert_is_not_held(self, tmp_path):
        _reset()
        _, bot, pending, held = await self._report(
            tmp_path, _raise(ValueError, "boom"), fail=False)
        bot.send_message.assert_awaited_once()
        assert held == [] and not pending.exists()

    async def test_a_suppressed_alert_is_not_held(self, tmp_path):
        """Nothing was built or attempted, so there is nothing to retry — a
        single transient blip must not become a queued DM."""
        _reset()
        from telegram.error import NetworkError
        _, bot, _, held = await self._report(
            tmp_path, _raise(NetworkError, "Bad Gateway"), fail=True)
        bot.send_message.assert_not_awaited()
        assert held == []

    async def test_the_held_text_is_the_real_alert(self, tmp_path):
        """A placeholder would pass every other test here and tell the admin
        nothing."""
        _reset()
        error_id, _, _, held = await self._report(
            tmp_path, _raise(ValueError, "boom"), fail=True)
        assert error_id in held[0]["text"]
        assert "ValueError: boom" in held[0]["text"]
        assert "errors.log" in held[0]["text"]

    def test_the_queue_keeps_the_oldest_on_overflow(self, tmp_path):
        """The first occurrence carries the context; later ones are usually the
        same fault repeating."""
        with _Pending(tmp_path):
            for i in range(er.MAX_PENDING + 5):
                er.hold_alert(f"ERR-{i:04d}", f"alert {i}")
            held = er._load_pending()
        assert len(held) == er.MAX_PENDING
        assert held[0]["error_id"] == "ERR-0000"
        assert held[-1]["error_id"] == f"ERR-{er.MAX_PENDING - 1:04d}"

    def test_reset_error_log_does_not_discard_held_alerts(self, tmp_path):
        """A restart is the LIKELIEST reason one is sitting there — the
        watchdog exits the process after a sustained outage. Clearing it at
        startup would discard exactly what this feature exists to keep."""
        with _Pending(tmp_path) as pending:
            _hold(pending)
            with patch.object(er, "ERRORS_LOG", tmp_path / "errors.log"):
                er.reset_error_log()
            assert len(er._load_pending()) == 1

    def test_a_corrupt_queue_does_not_raise(self, tmp_path):
        with _Pending(tmp_path) as pending:
            pending.write_text("{not json", encoding="utf-8")
            assert er._load_pending() == []
            er.hold_alert("ERR-NEW", "fresh alert")     # must still work
            assert [e["error_id"] for e in er._load_pending()] == ["ERR-NEW"]

    def test_entries_without_a_usable_stamp_are_dropped(self, tmp_path):
        import json
        with _Pending(tmp_path) as pending:
            pending.write_text(json.dumps(
                [{"error_id": "ERR-BAD", "queued_at": "not-a-date", "text": "x"}]),
                encoding="utf-8")
            assert er._fresh(er._load_pending(), er._now()) == []


class TestFlushingHeldAlerts:
    async def _flush(self, tmp_path, fail=False, users=None):
        bot = _ops_bot(fail=fail)
        u, ops_names, ops_ids = _as_ops(users)
        with u, ops_names, ops_ids:
            sent = await er.flush_pending_alerts(bot)
            held = er._load_pending()
        return sent, bot, held

    async def test_delivers_and_empties_the_queue(self, tmp_path):
        with _Pending(tmp_path) as pending:
            _hold(pending)
            sent, bot, held = await self._flush(tmp_path)
            assert sent == 1
            bot.send_message.assert_awaited_once()
            assert bot.send_message.await_args[0][0] == 7
            assert held == [] and not pending.exists()

    async def test_the_retry_says_it_is_delayed_and_when(self, tmp_path):
        """Without this the admin reads a two-hour-old traceback as a live
        incident and goes hunting a fault that already ended."""
        with _Pending(tmp_path) as pending:
            _hold(pending, age=timedelta(hours=2))
            _, bot, _ = await self._flush(tmp_path)
        text = bot.send_message.await_args[0][1]
        assert "Delayed alert" in text
        assert "could not reach" in text
        stamp = (er._now() - timedelta(hours=2)).strftime("%a %H:%M")
        assert stamp in text, f"expected the original time {stamp} in: {text!r}"
        assert "ValueError: boom" in text, "the original alert must survive"

    async def test_plain_text_on_retry_too(self, tmp_path):
        with _Pending(tmp_path) as pending:
            _hold(pending)
            _, bot, _ = await self._flush(tmp_path)
        assert "parse_mode" not in bot.send_message.await_args.kwargs

    async def test_a_still_failing_send_keeps_the_alert(self, tmp_path):
        """The original bug was emptying the queue against a dead connection.
        A failed retry must leave the entry exactly where it was."""
        with _Pending(tmp_path) as pending:
            _hold(pending)
            sent, bot, held = await self._flush(tmp_path, fail=True)
            assert sent == 0
            bot.send_message.assert_awaited()
            assert len(held) == 1 and held[0]["error_id"] == "ERR-HELD1"
            assert pending.exists()

    async def test_stops_at_the_first_failure_and_keeps_the_rest(self, tmp_path):
        """One success then a failure: the delivered one goes, the failing one
        and everything after it stay, in order."""
        with _Pending(tmp_path) as pending:
            _hold(pending, "ERR-FIRST")
            _hold(pending, "ERR-SECOND")
            _hold(pending, "ERR-THIRD")
            bot = MagicMock()
            calls = {"n": 0}

            async def _send(chat_id, text):
                calls["n"] += 1
                if calls["n"] > 1:
                    raise RuntimeError("Bad Gateway")

            bot.send_message = AsyncMock(side_effect=_send)
            u, ops_names, ops_ids = _as_ops()
            with u, ops_names, ops_ids:
                sent = await er.flush_pending_alerts(bot)
                held = er._load_pending()
        assert sent == 1
        assert [e["error_id"] for e in held] == ["ERR-SECOND", "ERR-THIRD"]

    async def test_expired_alerts_are_dropped_unsent(self, tmp_path):
        with _Pending(tmp_path) as pending:
            _hold(pending, age=er.PENDING_MAX_AGE + timedelta(minutes=1))
            sent, bot, held = await self._flush(tmp_path)
        assert sent == 0
        bot.send_message.assert_not_awaited()
        assert held == []

    async def test_a_fresh_alert_is_not_dropped_as_expired(self, tmp_path):
        """Guards the boundary from the other side: if the age check were
        inverted or the window zero, everything would silently expire."""
        with _Pending(tmp_path) as pending:
            _hold(pending, age=er.PENDING_MAX_AGE - timedelta(minutes=1))
            sent, bot, _ = await self._flush(tmp_path)
        assert sent == 1
        bot.send_message.assert_awaited_once()

    async def test_no_recipient_keeps_the_queue(self, tmp_path):
        """A retry with nobody to send to must not consume the alert."""
        with _Pending(tmp_path) as pending:
            _hold(pending)
            sent, bot, held = await self._flush(tmp_path, users=[])
        assert sent == 0
        bot.send_message.assert_not_awaited()
        assert len(held) == 1

    async def test_an_empty_queue_sends_nothing(self, tmp_path):
        with _Pending(tmp_path):
            sent, bot, _ = await self._flush(tmp_path)
        assert sent == 0
        bot.send_message.assert_not_awaited()

    async def test_end_to_end_outage_then_recovery(self, tmp_path):
        """The whole point, in one test: an alert that cannot get out during an
        outage is delivered once the network returns."""
        _reset()
        with _Pending(tmp_path), \
             patch.object(er, "ERRORS_LOG", tmp_path / "errors.log"):
            u, ops_names, ops_ids = _as_ops()
            down = _ops_bot(fail=True)
            with u, ops_names, ops_ids:
                error_id = await er.report_exception(down, _raise(ValueError, "boom"))
            u, ops_names, ops_ids = _as_ops()
            back = _ops_bot(fail=False)
            with u, ops_names, ops_ids:
                sent = await er.flush_pending_alerts(back)
                remaining = er._load_pending()
        assert sent == 1
        assert error_id in back.send_message.await_args[0][1]
        assert remaining == []


class TestHeldAlertsKeepTheirTraces:
    """Every alert ends "Full trace: logs/errors.log (search ERR-XXXXXX)".
    A restart clears errors.log, so an alert held ACROSS a restart would send
    the admin to an empty file — the one case where last run's traces are not
    stale, because nobody has seen them yet.
    """

    def test_the_held_alerts_trace_survives_the_reset(self, tmp_path):
        log = tmp_path / "errors.log"
        with _Pending(tmp_path) as pending, patch.object(er, "ERRORS_LOG", log):
            er.write_error_log("ERR-KEEP01", _raise(ValueError, "held fault"))
            er.write_error_log("ERR-GONE01", _raise(KeyError, "already reported"))
            _hold(pending, "ERR-KEEP01")
            er.reset_error_log()
            text = log.read_text()
        assert "ERR-KEEP01" in text and "held fault" in text
        assert "Traceback" in text, "the trace itself must be kept, not just the id"
        assert "ERR-GONE01" not in text, "delivered traces are still cleared"

    def test_nothing_held_still_clears_everything(self, tmp_path):
        """The normal case must not regress into never pruning the log."""
        log = tmp_path / "errors.log"
        with _Pending(tmp_path), patch.object(er, "ERRORS_LOG", log):
            er.write_error_log("ERR-AAA111", _raise(ValueError, "x"))
            discarded = er.reset_error_log()
            assert log.read_text() == ""
        assert discarded > 0

    def test_the_reported_byte_count_excludes_what_was_kept(self, tmp_path):
        log = tmp_path / "errors.log"
        with _Pending(tmp_path) as pending, patch.object(er, "ERRORS_LOG", log):
            er.write_error_log("ERR-KEEP01", _raise(ValueError, "held"))
            before = log.stat().st_size
            _hold(pending, "ERR-KEEP01")
            discarded = er.reset_error_log()
            after = log.stat().st_size
        assert after == before, "nothing should have been removed"
        assert discarded == 0, f"claimed {discarded} bytes discarded but kept all"

    def test_retain_keeps_whole_blocks_not_header_lines(self, tmp_path):
        text = ("===== ERR-ONE  stamp =====\nmessage: one\nbody one\n\n"
                "===== ERR-TWO  stamp =====\nmessage: two\nbody two\n\n")
        kept = er._retain_traces(text, {"ERR-TWO"})
        assert "body two" in kept
        assert "body one" not in kept and "ERR-ONE" not in kept

    def test_retain_with_no_ids_keeps_nothing(self, tmp_path):
        text = "===== ERR-ONE  stamp =====\nbody\n"
        assert er._retain_traces(text, set()) == ""


class TestEpisodeTracking:
    """The duration rule's own unit, checked directly rather than only through
    should_alert, so a failure says which half is wrong.
    """

    def test_a_lone_occurrence_is_zero_length(self):
        """This is what keeps a single error silent, now that there is no
        count threshold to do it."""
        _reset()
        from telegram.error import NetworkError
        assert er.episode_duration(_raise(NetworkError, "x"), er._now()) == timedelta(0)

    def test_duration_grows_with_the_episode(self):
        _reset()
        from telegram.error import NetworkError
        now = er._now()
        er.episode_duration(_raise(NetworkError, "x"), now)
        d = er.episode_duration(_raise(NetworkError, "x"), now + timedelta(minutes=2))
        assert d == timedelta(minutes=2)

    def test_a_gap_longer_than_episode_gap_resets_the_clock(self):
        _reset()
        from telegram.error import NetworkError
        now = er._now()
        er.episode_duration(_raise(NetworkError, "x"), now)
        after = now + er.EPISODE_GAP + timedelta(seconds=1)
        assert er.episode_duration(_raise(NetworkError, "x"), after) == timedelta(0)

    def test_a_gap_inside_episode_gap_extends_the_episode(self):
        _reset()
        from telegram.error import NetworkError
        now = er._now()
        er.episode_duration(_raise(NetworkError, "x"), now)
        inside = now + er.EPISODE_GAP - timedelta(seconds=1)
        assert er.episode_duration(_raise(NetworkError, "x"), inside) > timedelta(0)

    def test_distinct_fingerprints_track_separately(self):
        _reset()
        from telegram.error import NetworkError, TimedOut
        now = er._now()
        er.episode_duration(_raise(NetworkError, "x"), now)
        later = now + timedelta(minutes=4)
        er.episode_duration(_raise(NetworkError, "x"), later)
        assert er.episode_duration(_raise(TimedOut, "y"), later) == timedelta(0)

    def test_reset_clears_episode_state(self, tmp_path):
        """A restart means the old episode is over, so its age must not be
        inherited — a post-restart error would otherwise look like a condition
        that had already persisted and alert immediately.

        Armed with a TRANSIENT error on purpose: the sibling test in
        TestResetErrorLog uses a ValueError, which never touches _episodes, so
        it passed happily while reset left episode state behind.
        """
        _reset()
        from telegram.error import NetworkError
        er.episode_duration(_raise(NetworkError, "Bad Gateway"), er._now())
        assert er._episodes, "this test must arm the state it checks"
        with patch.object(er, "ERRORS_LOG", tmp_path / "errors.log"):
            er.reset_error_log()
        assert er._episodes == {}

    def test_alerting_does_not_resume_instantly_after_a_restart(self):
        """The consequence of the above, stated as behaviour."""
        _reset()
        from telegram.error import NetworkError
        now = er._now()
        for i in range(10):
            er.should_alert(_raise(NetworkError, "Bad Gateway"),
                            now=now + timedelta(seconds=30 * i))
        er._episodes.clear()          # as reset_error_log does on restart
        er._alert_state.clear()
        send, _ = er.should_alert(_raise(NetworkError, "Bad Gateway"),
                                  now=now + timedelta(minutes=10))
        assert send is False, (
            "after a restart the first transient error must start a fresh "
            "episode, not inherit the previous run's duration")

    def test_the_count_is_still_tracked_for_the_message(self):
        _reset()
        from telegram.error import NetworkError
        now = er._now()
        for i in range(3):
            er.episode_duration(_raise(NetworkError, "x"), now + timedelta(seconds=i))
        fp = er.fingerprint(_raise(NetworkError, "x"))
        assert er._episodes[fp]["count"] == 3

    def test_sustained_for_is_inside_the_watchdog_grace(self):
        """A sustained outage must be reported BEFORE network_watchdog
        restarts the process over it, or the report races the restart."""
        from network_watchdog import FAILURE_GRACE
        assert er.SUSTAINED_FOR < FAILURE_GRACE

    def test_episode_gap_exceeds_the_worst_measured_blip(self):
        """The longest nightly Telegram blip on record ran 1m 59s. If
        EPISODE_GAP were shorter, a single blip would fragment into several
        episodes and none would ever reach SUSTAINED_FOR."""
        assert er.EPISODE_GAP > timedelta(seconds=119)

    def test_the_alert_text_describes_duration_not_a_count(self):
        _reset()
        from telegram.error import NetworkError
        now = er._now()
        exc = _raise(NetworkError, "Bad Gateway")
        er.episode_duration(exc, now)
        text = er.build_alert("ERR-1", exc)
        assert "minutes" in text
        assert "Brief blips are not reported" in text

"""Tests for the three resilience fixes shipped after the 2026-09-04 outage.

1. systemd StartLimit* keys must sit in [Unit], where systemd actually reads
   them (they were silently ignored in [Service]).
2. The network watchdog: exit after a sustained Telegram outage so systemd can
   restart a bot that is wedged but alive.
3. cmd_unknown: reply to unrecognised commands instead of staying silent.
"""

from __future__ import annotations

import configparser
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import Forbidden, InvalidToken, NetworkError, TimedOut

import network_watchdog as nw
from network_watchdog import NetworkWatchdog, is_connectivity_error, watchdog_job
from settings import TZ

TEMPLATE = Path(__file__).resolve().parents[1] / "deploy" / "christofgod-bot.service.template"





def _at(hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return TZ.localize(datetime(2026, 9, 8, hh, mm, ss))


# ---------------------------------------------------------------------------
# 1. systemd unit file
# ---------------------------------------------------------------------------

class TestServiceTemplate:
    """The bug: these keys were in [Service], where systemd ignores them."""

    def _parse(self) -> configparser.ConfigParser:
        # strict=False: systemd permits repeated keys (e.g. ExecStart).
        cp = configparser.ConfigParser(strict=False)
        cp.optionxform = str  # systemd keys are case-sensitive
        cp.read_string(TEMPLATE.read_text(encoding="utf-8"))
        return cp

    @pytest.mark.parametrize("key", ["StartLimitIntervalSec", "StartLimitBurst"])
    def test_start_limit_keys_are_in_unit_section(self, key):
        cp = self._parse()
        assert cp.has_option("Unit", key), f"{key} must be in [Unit] for systemd to read it"

    @pytest.mark.parametrize("key", ["StartLimitIntervalSec", "StartLimitBurst"])
    def test_start_limit_keys_absent_from_service_section(self, key):
        cp = self._parse()
        assert not cp.has_option("Service", key), (
            f"{key} in [Service] is silently ignored by systemd — that was the bug"
        )

    def test_restart_directives_remain_in_service(self):
        """Restart/RestartSec genuinely belong in [Service]; don't over-correct."""
        cp = self._parse()
        assert cp.get("Service", "Restart") == "always"
        assert cp.has_option("Service", "RestartSec")

    def test_start_limit_window_exceeds_watchdog_grace(self):
        """Self-heal restarts must be too far apart to trip the start limit.

        If the limiter could be tripped by the watchdog, a long Telegram outage
        would permanently stop the bot instead of retrying it — the opposite of
        what both fixes are for.
        """
        cp = self._parse()
        window = timedelta(seconds=int(cp.get("Unit", "StartLimitIntervalSec")))
        assert nw.FAILURE_GRACE > window, (
            "watchdog grace must exceed the start-limit window, or a sustained "
            "outage would exhaust the restart budget and stop the bot for good"
        )


# ---------------------------------------------------------------------------
# 2. Watchdog decision logic (no I/O)
# ---------------------------------------------------------------------------

class TestNetworkWatchdog:
    def test_starts_healthy(self):
        assert NetworkWatchdog().failing is False

    def test_first_failure_does_not_trip(self):
        """One unlucky probe must never restart the bot."""
        w = NetworkWatchdog(grace=timedelta(minutes=10))
        assert w.record_failure(_at(12, 0)) is False
        assert w.failing is True

    def test_trips_only_after_grace_elapses(self):
        w = NetworkWatchdog(grace=timedelta(minutes=10))
        assert w.record_failure(_at(12, 0)) is False
        assert w.record_failure(_at(12, 5)) is False
        assert w.record_failure(_at(12, 9, 59)) is False
        assert w.record_failure(_at(12, 10)) is True

    def test_success_resets_the_clock(self):
        """A recovery mid-outage must not leave the bot primed to exit."""
        w = NetworkWatchdog(grace=timedelta(minutes=10))
        w.record_failure(_at(12, 0))
        w.record_failure(_at(12, 9))
        w.record_success(_at(12, 9, 30))
        assert w.failing is False
        # A fresh failure starts a new grace period rather than tripping.
        assert w.record_failure(_at(12, 10)) is False

    def test_success_reports_outage_length(self):
        w = NetworkWatchdog()
        w.record_failure(_at(12, 0))
        assert w.record_success(_at(12, 7)) == timedelta(minutes=7)

    def test_success_while_healthy_reports_zero(self):
        assert NetworkWatchdog().record_success(_at(12, 0)) == timedelta(0)

    def test_counts_consecutive_failures(self):
        w = NetworkWatchdog()
        for i in range(3):
            w.record_failure(_at(12, i))
        assert w.consecutive_failures == 3

    def test_outage_duration_zero_when_healthy(self):
        assert NetworkWatchdog().outage_duration(_at(12, 0)) == timedelta(0)


class TestIsConnectivityError:
    """Only "cannot reach Telegram" counts. A restart cannot fix the rest."""

    @pytest.mark.parametrize("exc", [
        NetworkError("boom"),
        TimedOut(),
        OSError("[Errno -3] Temporary failure in name resolution"),
    ])
    def test_connectivity_errors(self, exc):
        assert is_connectivity_error(exc) is True

    @pytest.mark.parametrize("exc", [
        InvalidToken(),
        Forbidden("blocked"),
        ValueError("not network related"),
    ])
    def test_non_connectivity_errors(self, exc):
        assert is_connectivity_error(exc) is False


# ---------------------------------------------------------------------------
# 3. Watchdog job (the wiring)
# ---------------------------------------------------------------------------

def _context(side_effect=None):
    ctx = MagicMock()
    ctx.bot.get_me = AsyncMock(side_effect=side_effect)
    ctx.application.stop_running = MagicMock()
    return ctx


@pytest.fixture(autouse=True)
def _fresh_watchdog():
    """Each test gets a clean module-level watchdog."""
    nw.watchdog = NetworkWatchdog()
    yield
    nw.watchdog = NetworkWatchdog()


class TestWatchdogJob:
    async def test_successful_probe_keeps_bot_running(self):
        ctx = _context()
        await watchdog_job(ctx)
        ctx.bot.get_me.assert_awaited_once()
        ctx.application.stop_running.assert_not_called()
        assert nw.watchdog.failing is False

    async def test_single_failure_does_not_stop_the_bot(self):
        ctx = _context(side_effect=NetworkError("dns"))
        await watchdog_job(ctx)
        ctx.application.stop_running.assert_not_called()
        assert nw.watchdog.failing is True

    async def test_sustained_outage_stops_the_application(self):
        """The 2026-09-04 scenario: reachable never returns."""
        ctx = _context(side_effect=OSError("Temporary failure in name resolution"))
        start = _at(12, 0)
        with patch.object(nw, "now_tz", side_effect=[start, start + timedelta(minutes=11)]):
            await watchdog_job(ctx)
            ctx.application.stop_running.assert_not_called()
            await watchdog_job(ctx)
        ctx.application.stop_running.assert_called_once()

    async def test_recovery_before_grace_never_stops_the_bot(self):
        """Telegram's brief 502s must not trigger a restart."""
        ctx = _context(side_effect=[NetworkError("502"), NetworkError("502"), None])
        for _ in range(3):
            await watchdog_job(ctx)
        ctx.application.stop_running.assert_not_called()
        assert nw.watchdog.failing is False

    async def test_non_connectivity_error_does_not_count_as_outage(self):
        """InvalidToken survives a restart; looping on it would be pointless."""
        ctx = _context(side_effect=InvalidToken())
        start = _at(12, 0)
        with patch.object(nw, "now_tz", side_effect=[start, start + timedelta(minutes=30)]):
            await watchdog_job(ctx)
            await watchdog_job(ctx)
        ctx.application.stop_running.assert_not_called()
        assert nw.watchdog.failing is False

    async def test_job_never_raises_when_application_missing(self):
        """A watchdog that crashes the bot it guards is worse than none."""
        ctx = _context(side_effect=NetworkError("dns"))
        ctx.application = None
        start = _at(12, 0)
        with patch.object(nw, "now_tz", side_effect=[start, start + timedelta(minutes=11)]):
            await watchdog_job(ctx)
            await watchdog_job(ctx)  # must not raise

    async def test_recovery_is_recorded_in_the_activity_log(self):
        ctx = _context(side_effect=[NetworkError("dns"), None])
        start = _at(12, 0)
        with patch.object(nw, "now_tz", side_effect=[start, start + timedelta(minutes=3)]):
            with patch.object(nw.activity, "log_error") as log_error:
                await watchdog_job(ctx)
                await watchdog_job(ctx)
        assert log_error.called
        assert "Recovered" in log_error.call_args[0][0]


# ---------------------------------------------------------------------------
# 4. Retrying ops alerts that could not get out
# ---------------------------------------------------------------------------

class TestHeldAlertsAreRetriedOnRecovery:
    """A successful probe is the only reliable signal that connectivity is
    back, so it is where held alerts get another chance. On 2026-10-27 the
    alert about Telegram 502s was itself lost to a 502 and simply dropped.
    """

    async def test_a_successful_probe_flushes_held_alerts(self):
        ctx = _context()
        with patch.object(nw.error_reporting, "flush_pending_alerts",
                          new=AsyncMock(return_value=1)) as flush:
            await watchdog_job(ctx)
        flush.assert_awaited_once_with(ctx.bot)

    async def test_a_failed_probe_does_not_try_to_flush(self):
        """The network is down; a send would only fail and burn the entry."""
        ctx = _context(side_effect=NetworkError("dns"))
        with patch.object(nw.error_reporting, "flush_pending_alerts",
                          new=AsyncMock()) as flush:
            await watchdog_job(ctx)
        flush.assert_not_awaited()

    async def test_flush_runs_after_a_recovery_too(self):
        ctx = _context(side_effect=[NetworkError("502"), None])
        with patch.object(nw.error_reporting, "flush_pending_alerts",
                          new=AsyncMock()) as flush:
            await watchdog_job(ctx)
            await watchdog_job(ctx)
        assert flush.await_count == 1

    async def test_a_broken_flush_never_costs_a_probe_cycle(self):
        """The watchdog guards the bot's life; failing to retry an alert must
        not stop it probing or let an exception escape the job."""
        ctx = _context()
        with patch.object(nw.error_reporting, "flush_pending_alerts",
                          new=AsyncMock(side_effect=RuntimeError("disk full"))):
            await watchdog_job(ctx)          # must not raise
        ctx.bot.get_me.assert_awaited_once()
        assert nw.watchdog.failing is False

    def test_error_reporting_does_not_import_this_module(self):
        """The dependency runs one way only. A cycle here would surface at
        import on the server rather than in CI, and the whole tree is walked
        because a deferred import inside a function would break the cycle at
        import time and reinstate it at call time."""
        import ast
        source = (Path(__file__).parent.parent / "error_reporting.py").read_text(
            encoding="utf-8")
        imported = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
        assert imported, "found no imports at all; this scan sees nothing"
        assert "network_watchdog" not in imported, (
            f"error_reporting must not import network_watchdog: {imported}")


class TestStartupDeliversWhatTheRestartInterrupted:
    """The leg that makes this feature work at all for a real outage.

    _give_up exits the process after FAILURE_GRACE so systemd restarts the
    bot, which destroys anything held only in memory — and a 10-minute-plus
    outage is precisely the case worth alerting about. So the queue is on disk
    and post_init is its second reader.
    """

    @staticmethod
    async def _post_init():
        import bot as botmod
        app = MagicMock()
        app.bot = MagicMock()
        app.bot.set_my_commands = AsyncMock()
        with patch.object(botmod, "schedule_all_upcoming", AsyncMock()), \
             patch.object(botmod, "alert_on_bad_service_links", AsyncMock()), \
             patch.object(botmod, "_register_jobs", MagicMock()), \
             patch.object(botmod.error_reporting, "reset_error_log",
                          MagicMock(return_value=0)) as reset, \
             patch.object(botmod.error_reporting, "flush_pending_alerts",
                          AsyncMock(return_value=0)) as flush:
            await botmod.post_init(app)
        return flush, reset, app

    async def test_post_init_flushes_held_alerts(self):
        flush, _reset_log, app = await self._post_init()
        flush.assert_awaited_once_with(app.bot)

    async def test_the_flush_happens_before_the_error_log_is_cleared(self):
        """Ordering is the whole point: the admin should learn why the bot
        restarted before startup does anything else that can fail."""
        order = []
        import bot as botmod
        app = MagicMock()
        app.bot = MagicMock()
        app.bot.set_my_commands = AsyncMock()

        async def _flush(bot):
            order.append("flush")
            return 0

        with patch.object(botmod, "schedule_all_upcoming", AsyncMock()), \
             patch.object(botmod, "alert_on_bad_service_links", AsyncMock()), \
             patch.object(botmod, "_register_jobs", MagicMock()), \
             patch.object(botmod.error_reporting, "reset_error_log",
                          lambda *a, **k: order.append("reset") or 0), \
             patch.object(botmod.error_reporting, "flush_pending_alerts",
                          side_effect=_flush):
            await botmod.post_init(app)
        assert order == ["flush", "reset"], order

    def test_reset_error_log_leaves_the_held_alerts_on_disk(self, tmp_path):
        """Checked here as well as in test_error_reporting because THIS is the
        sequence that would lose them: watchdog exits, systemd restarts,
        post_init clears last run's traces."""
        import error_reporting as er
        pending = tmp_path / "pending_alerts.json"
        with patch.object(er, "PENDING_ALERTS", pending):
            er.hold_alert("ERR-SURVIVE", "⚠️ Bot error ERR-SURVIVE")
            with patch.object(er, "ERRORS_LOG", tmp_path / "errors.log"):
                er.reset_error_log()
            assert [e["error_id"] for e in er._load_pending()] == ["ERR-SURVIVE"]

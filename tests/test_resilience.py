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
    @pytest.mark.asyncio
    async def test_successful_probe_keeps_bot_running(self):
        ctx = _context()
        await watchdog_job(ctx)
        ctx.bot.get_me.assert_awaited_once()
        ctx.application.stop_running.assert_not_called()
        assert nw.watchdog.failing is False

    @pytest.mark.asyncio
    async def test_single_failure_does_not_stop_the_bot(self):
        ctx = _context(side_effect=NetworkError("dns"))
        await watchdog_job(ctx)
        ctx.application.stop_running.assert_not_called()
        assert nw.watchdog.failing is True

    @pytest.mark.asyncio
    async def test_sustained_outage_stops_the_application(self):
        """The 2026-09-04 scenario: reachable never returns."""
        ctx = _context(side_effect=OSError("Temporary failure in name resolution"))
        start = _at(12, 0)
        with patch.object(nw, "now_tz", side_effect=[start, start + timedelta(minutes=11)]):
            await watchdog_job(ctx)
            ctx.application.stop_running.assert_not_called()
            await watchdog_job(ctx)
        ctx.application.stop_running.assert_called_once()

    @pytest.mark.asyncio
    async def test_recovery_before_grace_never_stops_the_bot(self):
        """Telegram's brief 502s must not trigger a restart."""
        ctx = _context(side_effect=[NetworkError("502"), NetworkError("502"), None])
        for _ in range(3):
            await watchdog_job(ctx)
        ctx.application.stop_running.assert_not_called()
        assert nw.watchdog.failing is False

    @pytest.mark.asyncio
    async def test_non_connectivity_error_does_not_count_as_outage(self):
        """InvalidToken survives a restart; looping on it would be pointless."""
        ctx = _context(side_effect=InvalidToken())
        start = _at(12, 0)
        with patch.object(nw, "now_tz", side_effect=[start, start + timedelta(minutes=30)]):
            await watchdog_job(ctx)
            await watchdog_job(ctx)
        ctx.application.stop_running.assert_not_called()
        assert nw.watchdog.failing is False

    @pytest.mark.asyncio
    async def test_job_never_raises_when_application_missing(self):
        """A watchdog that crashes the bot it guards is worse than none."""
        ctx = _context(side_effect=NetworkError("dns"))
        ctx.application = None
        start = _at(12, 0)
        with patch.object(nw, "now_tz", side_effect=[start, start + timedelta(minutes=11)]):
            await watchdog_job(ctx)
            await watchdog_job(ctx)  # must not raise

    @pytest.mark.asyncio
    async def test_recovery_is_recorded_in_the_activity_log(self):
        ctx = _context(side_effect=[NetworkError("dns"), None])
        start = _at(12, 0)
        with patch.object(nw, "now_tz", side_effect=[start, start + timedelta(minutes=3)]):
            with patch.object(nw.activity, "log_error") as log_error:
                await watchdog_job(ctx)
                await watchdog_job(ctx)
        assert log_error.called
        assert "Recovered" in log_error.call_args[0][0]

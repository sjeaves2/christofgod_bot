"""Detect sustained loss of Telegram connectivity and exit so systemd restarts.

Why this exists
---------------
On 2026-09-04 the bot went silent for 87 minutes. Nothing crashed. Memory
pressure on the VM killed DNS, and the bot sat in its polling loop retrying an
unresolvable hostname 510 times. `Restart=always` never fired, because
`Restart=always` only helps when a process *dies* — systemd saw a perfectly
healthy service and did nothing. A wedged-but-alive process is invisible to it.

So the bot has to notice on its own. This module probes the Telegram API on a
timer; when every probe has failed for a sustained stretch, it stops the
application deliberately, turning an invisible hang into a visible restart that
systemd already knows how to handle.

Design notes
------------
* The probe is an *active* call (`get_me`), not a count of errors seen
  elsewhere. A bot nobody is talking to generates no traffic and therefore no
  errors, so a passive error count could stay at zero right through an outage.
* Only connectivity failures count. `InvalidToken` or `Forbidden` mean a
  restart would change nothing, and looping on them forever would turn a
  configuration mistake into an endless restart cycle.
* The grace period is deliberately long. Telegram issues brief 502s (two on
  2026-09-05, rode out fine) and PTB's own retry logic handles those. Restarting
  for a blip would be worse than the blip.
* `NetworkWatchdog` holds no I/O so the decision rule can be tested directly;
  `watchdog_job` is the thin shell that talks to the network.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from telegram.error import NetworkError, TelegramError
from telegram.ext import ContextTypes

from common import now_tz
from settings import activity

logger = logging.getLogger(__name__)

# How often to probe. Frequent enough to notice promptly, cheap enough to be
# invisible: get_me is a few hundred bytes.
CHECK_INTERVAL = timedelta(minutes=1)

# How long everything must stay broken before we act. Comfortably longer than a
# Telegram hiccup or a systemd-resolved restart, comfortably shorter than the
# 87-minute outage that prompted this.
FAILURE_GRACE = timedelta(minutes=10)

# Distinct exit code so `systemctl status` / journal reads make it obvious the
# bot chose to leave rather than crashed. systemd restarts either way.
EXIT_CODE = 75  # EX_TEMPFAIL


class NetworkWatchdog:
    """Tracks how long the Telegram API has been continuously unreachable."""

    def __init__(self, grace: timedelta = FAILURE_GRACE) -> None:
        self.grace = grace
        self.first_failure_at: "datetime | None" = None
        self.consecutive_failures = 0

    @property
    def failing(self) -> bool:
        return self.first_failure_at is not None

    def outage_duration(self, now: datetime) -> timedelta:
        """How long the current outage has run; zero when healthy."""
        if self.first_failure_at is None:
            return timedelta(0)
        return now - self.first_failure_at

    def record_success(self, now: "datetime | None" = None) -> timedelta:
        """Clear the outage. Returns how long it had lasted (zero if healthy)."""
        now = now or now_tz()
        recovered_after = self.outage_duration(now)
        self.first_failure_at = None
        self.consecutive_failures = 0
        return recovered_after

    def record_failure(self, now: "datetime | None" = None) -> bool:
        """Note a failed probe. True once the grace period has been exhausted.

        The first failure starts the clock rather than tripping immediately, so
        a single unlucky probe can never restart the bot.
        """
        now = now or now_tz()
        if self.first_failure_at is None:
            self.first_failure_at = now
        self.consecutive_failures += 1
        return self.outage_duration(now) >= self.grace


# Module-level instance: one bot process, one watchdog. Tests construct their
# own NetworkWatchdog rather than leaning on this.
watchdog = NetworkWatchdog()


def is_connectivity_error(exc: BaseException) -> bool:
    """Whether *exc* means "cannot reach Telegram" rather than "Telegram said no".

    A restart can only fix the former. `InvalidToken`, `Forbidden` and friends
    would survive one, so they must not drive the watchdog.
    """
    if isinstance(exc, NetworkError):
        return True
    if isinstance(exc, TelegramError):
        return False
    # DNS failures surface as OSError/socket.gaierror before PTB wraps them.
    return isinstance(exc, OSError)


async def watchdog_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Probe Telegram; stop the application after a sustained outage.

    Never raises: a watchdog that crashes the thing it guards is worse than no
    watchdog at all.
    """
    now = now_tz()
    try:
        await context.bot.get_me()
    except BaseException as exc:  # noqa: BLE001 - deliberately broad; see below
        if not is_connectivity_error(exc):
            # Telegram answered and refused. Connectivity is fine, so this is
            # not the watchdog's problem; the error handler will report it.
            logger.debug("Watchdog probe rejected (not a connectivity fault): %s", exc)
            watchdog.record_success(now)
            return

        exhausted = watchdog.record_failure(now)
        elapsed = watchdog.outage_duration(now)
        logger.warning(
            "Telegram unreachable for %s (%d consecutive probe failures): %s",
            _human(elapsed), watchdog.consecutive_failures, exc,
        )
        if exhausted:
            _give_up(context, elapsed, exc)
        return

    recovered_after = watchdog.record_success(now)
    if recovered_after > timedelta(0):
        logger.info("Telegram reachable again after %s.", _human(recovered_after))
        activity.log_error(
            f"Recovered: Telegram was unreachable for {_human(recovered_after)}")


def _give_up(context: ContextTypes.DEFAULT_TYPE, elapsed: timedelta,
             exc: BaseException) -> None:
    """Log loudly, then ask the application to stop so systemd can restart it."""
    message = (
        f"Telegram unreachable for {_human(elapsed)} "
        f"({watchdog.consecutive_failures} failed probes; last error: "
        f"{type(exc).__name__}: {exc}). Exiting so systemd restarts the bot."
    )
    logger.critical(message)
    # Written to the activity log rather than DM'd: the DM would have to travel
    # over the very network that is broken.
    activity.log_error(message)

    app = getattr(context, "application", None)
    stop = getattr(app, "stop_running", None)
    if stop is None:
        logger.error("No application to stop; the watchdog cannot restart the bot.")
        return
    stop()


def _human(delta: timedelta) -> str:
    """'12m 30s' — short enough for a log line."""
    total = int(delta.total_seconds())
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"

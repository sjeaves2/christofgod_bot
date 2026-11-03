"""Test isolation for on-disk side effects.

Handlers log through the shared ActivityLogger, error reporting appends to
logs/errors.log, and an alert that cannot be sent is queued to
logs/pending_alerts.json. Without isolation a test run writes fake commands,
fake exceptions and fake ops alerts into the *production* logs — which then
show up in /stats as if real people had used the bot. (That is exactly what
happened: thousands of test-generated COMMAND lines and 'ValueError: boom'
errors accumulated in logs/bot_activity.log.)

The autouse fixture below redirects all three to a temporary directory for the
whole session, so the suite can never touch the real logs again.

PENDING_ALERTS is the one that got away, and it is worth knowing why: this
fixture was written before the held-alert queue existed, and adding the queue
in v0.24.1 did not add it here. Nothing failed, because logs/ is gitignored —
the pollution never appeared in git status. It surfaced only when a watchdog
change made two unrelated tests start queueing alerts, and a local bot start
would have DM'd those fakes to a real admin at post_init.

So: a new file under logs/ belongs in this fixture on the same commit that
creates it. Isolation that covers most of the paths is the kind that fails
quietly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True, scope="session")
def _isolate_logs(tmp_path_factory):
    """Point the activity log and error log at a throwaway directory."""
    import error_reporting
    import settings
    from activity_logger import ActivityLogger

    log_dir = tmp_path_factory.mktemp("logs")

    real_activity = settings.activity
    test_activity = ActivityLogger(log_dir, retention_days=settings.LOG_RETENTION,
                                   tz=settings.TZ)
    real_errors_log = error_reporting.ERRORS_LOG
    real_pending = error_reporting.PENDING_ALERTS

    # Rebind everywhere the logger was imported by value.
    settings.activity = test_activity
    error_reporting.activity = test_activity
    error_reporting.ERRORS_LOG = log_dir / "errors.log"
    error_reporting.PENDING_ALERTS = log_dir / "pending_alerts.json"
    for name in ("bot", "handlers.appointments", "handlers.announcements",
                 "handlers.broadcast", "handlers.events_admin",
                 "handlers.notifications", "handlers.user_basics",
                 "handlers.stats"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "activity"):
            module.activity = test_activity

    yield

    settings.activity = real_activity
    error_reporting.activity = real_activity
    error_reporting.ERRORS_LOG = real_errors_log
    error_reporting.PENDING_ALERTS = real_pending

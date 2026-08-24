"""Test isolation for on-disk side effects.

Handlers log through the shared ActivityLogger, and error reporting appends to
logs/errors.log. Without isolation a test run writes fake commands and fake
exceptions into the *production* logs — which then show up in /stats as if
real people had used the bot. (That is exactly what happened: thousands of
test-generated COMMAND lines and 'ValueError: boom' errors accumulated in
logs/bot_activity.log.)

The autouse fixture below redirects both to a temporary directory for the whole
session, so the suite can never touch the real logs again.
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

    # Rebind everywhere the logger was imported by value.
    settings.activity = test_activity
    error_reporting.activity = test_activity
    error_reporting.ERRORS_LOG = log_dir / "errors.log"
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

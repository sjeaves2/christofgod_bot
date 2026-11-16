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

PENDING_ALERTS was not the last one. On 2026-11-11 a suite run was measured
appending 99 lines to the real logs/bot_activity.log, because the rebinding
below was a HARDCODED LIST of eight modules and six others import the logger
by value. The list is now derived by identity from sys.modules, and
tests/test_error_reporting.py asserts that no loaded module holds a logger
writing inside the repository. Prefer deriving over listing anywhere in this
file: the list and the thing it tracks are updated by different people on
different days.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True, scope="session")
def _no_network():
    """Make it impossible for the suite to reach the network.

    Every test file here claims to be offline, and twice on 2026-11-16 that
    claim was false: apple_music.fetch_artwork and then fetch_preview were
    added to the preview path and the worship tests did not substitute them,
    so the suite really issued HTTP requests to fake URLs like
    `is1-ssl.mzstatic.com/a/300x300bb.jpg` and took 403s. Both functions keep
    a never-raises contract, so the failures were swallowed into None and
    almost everything still passed — the leak showed up only as a 403 in the
    captured log of an unrelated failure.

    That is the shape of problem a per-test patch cannot fix. The first fix
    was "remember to patch the new function", which is the hardcoded-list
    mistake that cost v0.28.16 and v0.28.17 in a different costume: a guard
    that covers what someone remembered. So the socket layer is closed here
    instead, once, for the whole session.

    Why sockets and not httpx: a test that forgets to patch a fetcher should
    FAIL, not quietly succeed against a substituted transport. Blocking at
    the bottom means the error names the socket and the traceback shows which
    call tried to leave.

    Loopback is left open deliberately. Nothing here needs it today, but
    pytest plugins and debuggers do, and breaking the tooling to enforce a
    rule about Apple's CDN would be a bad trade. 127.0.0.1 cannot reach
    itunes.apple.com.
    """
    import socket

    real_socket = socket.socket
    real_create = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    _LOCAL = {"127.0.0.1", "::1", "localhost", ""}

    def _check(host):
        if isinstance(host, (bytes, bytearray)):
            host = host.decode("ascii", "replace")
        if str(host) not in _LOCAL:
            raise RuntimeError(
                f"A test tried to open a network connection to {host!r}. "
                f"The suite must be offline: substitute the fetcher (see "
                f"tests/test_apple_music.py for the pattern) rather than "
                f"letting a real request leave. If this is a NEW outbound "
                f"call, every test that reaches it needs patching — not just "
                f"the one you are looking at."
            )

    class _Socket(real_socket):
        def connect(self, address, *a, **k):
            _check(address[0] if isinstance(address, tuple) else address)
            return super().connect(address, *a, **k)

        def connect_ex(self, address, *a, **k):
            _check(address[0] if isinstance(address, tuple) else address)
            return super().connect_ex(address, *a, **k)

    def _create_connection(address, *a, **k):
        _check(address[0] if isinstance(address, tuple) else address)
        return real_create(address, *a, **k)

    def _getaddrinfo(host, *a, **k):
        _check(host)
        return real_getaddrinfo(host, *a, **k)

    socket.socket = _Socket
    socket.create_connection = _create_connection
    socket.getaddrinfo = _getaddrinfo
    try:
        yield
    finally:
        socket.socket = real_socket
        socket.create_connection = real_create
        socket.getaddrinfo = real_getaddrinfo


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
    #
    # DERIVED, not listed. This used to name eight modules by hand and missed
    # six: handlers.board, handlers.prayer, handlers.worship, handlers.backup,
    # handlers.shared and network_watchdog all do `from settings import
    # activity`, so they kept the REAL logger and a full suite run appended ~99
    # lines to the production logs/bot_activity.log — mostly `[COMMAND] Board`.
    # deploy/DEPLOY.md claimed isolation prevented that; it did not.
    #
    # The docstring of TestTheSuiteCannotTouchTheRealLogs warned about exactly
    # this: "a list would need the same update conftest needs, and would be
    # forgotten in the same breath." It was, in the same file.
    #
    # Identity (`is real_activity`) is the test, not hasattr: it finds every
    # module holding THIS object however it was imported, and skips modules
    # that legitimately hold something else. Modules imported after this
    # fixture runs are already safe, because they read the rebound
    # settings.activity.
    settings.activity = test_activity
    error_reporting.ERRORS_LOG = log_dir / "errors.log"
    error_reporting.PENDING_ALERTS = log_dir / "pending_alerts.json"

    rebound = []
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        try:
            if getattr(module, "activity", None) is real_activity:
                module.activity = test_activity
                rebound.append(name)
        except Exception:  # noqa: BLE001 - a lazy module must not break isolation
            continue

    yield

    for name in rebound:
        module = sys.modules.get(name)
        if module is not None:
            module.activity = real_activity
    settings.activity = real_activity
    error_reporting.ERRORS_LOG = real_errors_log
    error_reporting.PENDING_ALERTS = real_pending

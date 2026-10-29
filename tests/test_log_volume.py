"""apscheduler must not narrate every job tick into bot.log.

Measured on the live server 2026-10-29: of **259,251 lines in a 45 MB
logs/bot.log, 253,256 (97.7%) came from apscheduler** —
`apscheduler.executors.default` logging "Running job X" and "Job X executed
successfully" for a watchdog that fires every minute, a catch-up every two, and
appointment reminders every five, plus `apscheduler.scheduler` logging "Added
job"/"Removed job" for all 179 notification jobs on every reschedule.

Everything the bot itself logged in the same file came to about 650 lines, and
logs/bot_activity.log — the log with actual content — was 64 KB.

The cost was not only disk. The pruner that `settings` used to run at import
read the whole file every time, so the volume became ~9 seconds of every
startup and a 45 MB allocation on a 412 MB box whose one outage was caused by
memory exhaustion. Rotation (see tests/test_log_rotation.py) fixed the reading;
this fixes the writing.

These tests assert the SUPPRESSION BEHAVIOUR, not the configuration: what
matters is that an INFO record from apscheduler's children is never created,
and that the bot's own INFO logging is untouched.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO = Path(__file__).resolve().parents[1]

#: The loggers that produced 97.7% of the volume. Named explicitly: suppressing
#: the parent only helps because these have no level of their own.
NOISY = ("apscheduler.executors.default", "apscheduler.scheduler")

#: Loggers whose INFO output is the reason bot.log exists at all.
WANTED = ("handlers.notifications", "handlers.board", "bot", "settings",
          "telegram.ext.Application")


@pytest.fixture(autouse=True)
def _settings_imported():
    """Importing settings is what applies the policy."""
    import settings  # noqa: F401


class TestApschedulerIsQuiet:
    @pytest.mark.parametrize("name", NOISY)
    def test_info_records_are_not_emitted(self, name):
        """The behavioural assertion. Mutation check: deleting the setLevel
        call in settings.py makes this fail."""
        assert not logging.getLogger(name).isEnabledFor(logging.INFO), (
            f"{name} still logs at INFO — this is 97.7% of bot.log"
        )

    @pytest.mark.parametrize("name", NOISY)
    def test_warnings_and_errors_still_get_through(self, name):
        """A scheduler that drops jobs or misfires must still be able to say
        so. Suppressing those would trade a disk problem for a blind spot."""
        log = logging.getLogger(name)
        assert log.isEnabledFor(logging.WARNING)
        assert log.isEnabledFor(logging.ERROR)

    def test_the_children_inherit_rather_than_being_set_individually(self):
        """Why setting the parent is enough — and a guard against apscheduler
        starting to set its own levels in some future version, which would
        silently undo this."""
        for name in NOISY:
            assert logging.getLogger(name).level == logging.NOTSET, (
                f"{name} now has an explicit level; the parent setting no "
                "longer governs it"
            )
        assert logging.getLogger("apscheduler").level == logging.WARNING


class TestTheBotsOwnLoggingIsUntouched:
    """The failure mode to avoid is quieting too broadly.

    These assert that settings sets NO level on these loggers, so each one
    still resolves through the root logger — which settings puts at DEBUG in
    production so that demoted records can reach the handlers.

    Why not simply assert `isEnabledFor(logging.INFO)`: **pytest configures the
    root logger before conftest imports settings**, which makes settings'
    `logging.basicConfig(level=DEBUG, ...)` a no-op for the whole suite —
    basicConfig does nothing once root has handlers. Under pytest root sits at
    WARNING, so an effective-level assertion here measures pytest's
    configuration rather than this project's. That is this repo's vacuity
    pattern #2, reading ambient state instead of constructing it, and it caught
    me again on 2026-10-29. An explicit setLevel (which is how apscheduler is
    suppressed) is unaffected by that, which is why the tests above CAN be
    behavioural.
    """

    @pytest.mark.parametrize("name", WANTED)
    def test_no_level_is_imposed_on_them(self, name):
        """Mutation check: suppressing "telegram" or the root logger instead of
        "apscheduler" makes this fail."""
        assert logging.getLogger(name).level == logging.NOTSET, (
            f"{name} has an explicit level of "
            f"{logging.getLevelName(logging.getLogger(name).level)}; it should "
            "inherit from root"
        )

    def test_the_deploy_marker_is_not_suppressed(self):
        """deploy/update.sh waits for "Application started" from
        telegram.ext.Application at INFO. Suppressing that logger — or any
        ancestor of it — would make every deploy report failure."""
        for name in ("telegram", "telegram.ext", "telegram.ext.Application"):
            level = logging.getLogger(name).level
            assert level in (logging.NOTSET, logging.DEBUG, logging.INFO), (
                f"{name} is set to {logging.getLevelName(level)}, which hides "
                "the marker the deploy script waits for"
            )

    def test_the_root_logger_is_not_suppressed_in_the_source(self):
        """Complements the above: the production root level cannot be observed
        under pytest, so assert what settings asks for. Parses the call, not
        the file — the comments mention both DEBUG and WARNING."""
        source = (REPO / "settings.py").read_text(encoding="utf-8")
        call = re.search(r"logging\.basicConfig\(\s*level=([^,]+),", source)
        assert call, "basicConfig is no longer recognisable"
        assert call.group(1).strip() == "logging.DEBUG", call.group(1)


class TestItIsRecoverable:
    def test_debug_verbosity_brings_the_scheduler_back(self):
        """The same bargain the httpx filter strikes: hidden by default, fully
        available at log.level=DEBUG.

        NOT behavioural — settings applies the policy at import and reloading
        it would reconfigure global logging mid-suite. So this parses the
        setLevel CALL rather than the file: the comment above it mentions both
        DEBUG and WARNING, so grepping the file for either word would match the
        prose whatever the code did. (That trap has produced two vacuous tests
        in this repo already.)
        """
        source = (REPO / "settings.py").read_text(encoding="utf-8")
        call = re.search(
            r'logging\.getLogger\("apscheduler"\)\.setLevel\(\s*(.+?)\)\s*$',
            source, re.M | re.S)
        assert call, "the apscheduler setLevel call is no longer recognisable"
        expr = " ".join(call.group(1).split())
        assert expr == ("logging.DEBUG if LOG_LEVEL <= logging.DEBUG "
                        "else logging.WARNING"), expr

    def test_the_measurement_is_recorded_where_someone_will_see_it(self):
        """Someone will eventually want to re-enable this. The number is the
        argument, so it belongs next to the code, not only in a commit."""
        source = (REPO / "settings.py").read_text(encoding="utf-8")
        assert "97.7%" in source

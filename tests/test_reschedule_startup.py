"""Event notifications are scheduled exactly once per startup.

`post_init` calls `schedule_all_upcoming()`, and the weekly_reschedule job used
to be registered with `first=timedelta(seconds=10)` — so ten seconds after
every start it removed and re-added all ~180 notification jobs that call had
just created. Measured on the 2026-10-29 restart:

    12:43:07.196  Scheduled 180 upcoming event notifications   <- post_init
    12:43:17.484  Scheduled 180 upcoming event notifications   <- the 10s job

Pure duplication, and the source of the 32,592 "Added job"/"Removed job" lines
that were a third of a 45 MB bot.log.

Removing it is only correct if post_init's jobs actually survive, and post_init
runs BEFORE `Application.start()` — so before the JobQueue's scheduler is
running. `TestJobsAddedBeforeTheQueueStarts` establishes that premise against
a real JobQueue rather than trusting the documentation, because the whole
change rests on it: if those jobs were dropped, the 10-second pass would be the
only thing scheduling anything and removing it would silence every reminder.
"""

from __future__ import annotations

import asyncio
import datetime
import sys
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.ext import ApplicationBuilder

sys.path.insert(0, str(Path(__file__).parent.parent))

#: The designated synthetic token (see tests/test_no_secrets_committed.py).
FAKE_TOKEN = "111111111:AAAA-NOT-A-REAL-TOKEN-FOR-TESTS-ONLY"


class TestJobsAddedBeforeTheQueueStarts:
    """The premise. APScheduler holds jobs added before start and flushes them
    when the scheduler starts, so post_init's pass is not lost."""

    async def test_a_job_added_before_start_still_fires(self):
        fired: list[str] = []
        app = ApplicationBuilder().token(FAKE_TOKEN).build()
        jq = app.job_queue

        async def cb(context):
            fired.append(context.job.name)

        jq.run_once(cb, when=datetime.timedelta(seconds=0.2), name="before_start")
        assert not jq.scheduler.running, "the premise requires a stopped scheduler"

        await jq.start()
        await asyncio.sleep(0.8)
        await jq.stop()

        assert fired == ["before_start"], (
            "a job added before the queue started was dropped — post_init's "
            "scheduling pass would be lost and the weekly job is load-bearing"
        )

    async def test_such_a_job_is_visible_immediately(self):
        app = ApplicationBuilder().token(FAKE_TOKEN).build()
        jq = app.job_queue

        async def cb(context):
            pass

        jq.run_once(cb, when=datetime.timedelta(seconds=30), name="pending")
        assert [j.name for j in jq.jobs()] == ["pending"]
        assert not jq.scheduler.running


class TestTheWeeklyJobDoesNotRunAtStartup:
    """What the change actually is."""

    @staticmethod
    def _jobs():
        """The run_repeating/run_daily registrations post_init makes."""
        import bot as botmod

        repeating: list[dict] = []
        daily: list[dict] = []
        captured: dict = {}

        class FakeApp:
            def __init__(self):
                self.job_queue = MagicMock()
                self.job_queue.run_repeating = (
                    lambda cb, interval=None, first=None, name=None, **k:
                    repeating.append({"cb": getattr(cb, "__name__", str(cb)),
                                      "interval": interval, "first": first,
                                      "name": name}))
                self.job_queue.run_daily = (
                    lambda cb, time=None, name=None, **k:
                    daily.append({"cb": getattr(cb, "__name__", str(cb)),
                                  "time": time, "name": name}))
                self.bot = MagicMock()
                self.bot.set_my_commands = AsyncMock()

            def add_handler(self, h, group=0):
                pass

            def add_error_handler(self, h):
                pass

            def run_polling(self, **kw):
                pass

        fake = FakeApp()

        class FakeBuilder:
            def token(self, t):
                return self

            def post_init(self, f):
                captured["post_init"] = f
                return self

            def build(self):
                return fake

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with patch.object(botmod.Application, "builder",
                              staticmethod(lambda: FakeBuilder())):
                botmod.main()

        assert "post_init" in captured, "main() no longer registers a post_init"
        with patch.object(botmod, "schedule_all_upcoming", AsyncMock()), \
             patch.object(botmod, "alert_on_bad_service_links", AsyncMock()), \
             patch.object(botmod.error_reporting, "reset_error_log",
                          lambda *a, **k: 0):
            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                captured["post_init"](fake))
        return repeating, daily

    def _weekly(self):
        repeating, _daily = self._jobs()
        assert repeating, "no repeating jobs registered — this test sees nothing"
        job = next((j for j in repeating if j["name"] == "weekly_reschedule"), None)
        assert job, [j["name"] for j in repeating]
        return job

    def test_it_does_not_fire_shortly_after_startup(self):
        """The regression. `first=10s` meant a duplicate pass on every start."""
        first = self._weekly()["first"]
        assert first is None or first >= datetime.timedelta(days=7), (
            f"weekly_reschedule is set to first run after {first}; post_init "
            "already schedules everything, so this duplicates it"
        )

    def test_it_still_repeats_weekly(self):
        """Removing the startup run must not remove the job. An unrestarted
        process relies on it to pull newly-dated events into the 400-day
        window."""
        assert self._weekly()["interval"] == datetime.timedelta(days=7)

    def test_it_still_calls_the_reschedule_callback(self):
        assert self._weekly()["cb"] == "reschedule_job"


class TestPostInitSchedulesOnce:
    async def test_post_init_calls_schedule_all_upcoming_exactly_once(self):
        """The initial pass is post_init's job and nobody else's."""
        import bot as botmod

        app = MagicMock()
        app.job_queue = MagicMock()
        app.bot.set_my_commands = AsyncMock()

        with patch.object(botmod, "schedule_all_upcoming", AsyncMock()) as sched, \
             patch.object(botmod, "alert_on_bad_service_links", AsyncMock()), \
             patch.object(botmod.error_reporting, "reset_error_log",
                          lambda *a, **k: 0):
            await botmod.post_init(app)
        assert sched.await_count == 1, sched.await_count

    async def test_the_reschedule_job_does_the_same_work(self):
        """So the weekly run is a genuine substitute for a startup run, which
        is why dropping the startup run loses nothing."""
        import bot as botmod

        ctx = MagicMock()
        with patch.object(botmod, "schedule_all_upcoming", AsyncMock()) as sched:
            await botmod.reschedule_job(ctx)
        sched.assert_awaited_once_with(ctx.application)


class TestReschedulingIsIdempotent:
    """Why re-running the pass is safe at all: each event's job is removed
    before being re-added, keyed by the event's own stable key."""

    def test_an_existing_job_is_removed_before_being_re_added(self):
        from handlers.notifications import schedule_event_notification

        app = MagicMock()
        existing = MagicMock()
        app.job_queue.get_jobs_by_name.return_value = [existing]
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)

        schedule_event_notification(app, {"key": "k1", "notification_time": future})

        existing.schedule_removal.assert_called_once()
        app.job_queue.run_once.assert_called_once()
        assert app.job_queue.run_once.call_args.kwargs["name"] == "notif_k1"

    def test_a_past_notification_is_not_scheduled(self):
        """Otherwise a restart would fire every reminder whose moment has
        already gone."""
        from handlers.notifications import schedule_event_notification

        app = MagicMock()
        app.job_queue.get_jobs_by_name.return_value = []
        past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)

        schedule_event_notification(app, {"key": "old", "notification_time": past})
        app.job_queue.run_once.assert_not_called()

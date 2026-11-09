"""An alert that points at a trace must not outlive the trace.

Every alert from report_exception ends with

    Full trace: logs/errors.log (search ERR-XXXXXX)

and post_init does two things in this order:

    await flush_pending_alerts(app.bot)   # delivers held alerts
    reset_error_log()                     # clears last run's traces

A delivered alert is REMOVED from the pending store, so by the time
reset_error_log asks "which traces belong to alerts still waiting?", the alert
that just went out is no longer in that list — and its trace was deleted. The
admin received a message telling them to search a file that had been emptied
of exactly the thing they were told to search for.

reset_error_log's docstring claimed the opposite before v0.28.12 ("The traces
those held alerts point at are kept too"). It was true only for alerts that
failed to send AGAIN, which is the narrower case it was written for.

A dangling pointer in an alert is worse than no pointer: the reader concludes
the logging is broken, or that they misread the id, and stops trusting the
alert that did arrive.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import error_reporting as er  # noqa: E402

FLUSHED = "ERR-AAA111"       # held, then delivered on this start
STILL_HELD = "ERR-BBB222"    # held, and the send fails again
STALE = "ERR-CCC333"         # last run's noise, nothing points at it


def _write_log(tmp_path, *error_ids):
    """An errors.log holding one trace block per id, as write_error_log does."""
    log = tmp_path / "errors.log"
    log.write_text("".join(
        f"===== {eid}  2026-11-09 12:00:0{n} EST =====\n"
        f"Traceback for {eid}\n"
        f"  line one\n\n"
        for n, eid in enumerate(error_ids)
    ), encoding="utf-8")
    return log


def _entry(error_id):
    """A pending entry exactly as hold_alert writes one.

    The field is `queued_at`, not `held_at`: `_fresh` drops any entry whose
    stamp it cannot read, so a wrong key here makes the flush silently deliver
    nothing and the test prove nothing. That is why
    test_a_delivered_alert_keeps_its_trace asserts something WAS sent before
    asserting anything about the log.
    """
    return {"error_id": error_id,
            "queued_at": er._now().isoformat(),
            "text": f"alert for {error_id}\n"
                    f"Full trace: logs/errors.log (search {error_id})"}


class TestTheTraceSurvivesItsAlert:
    async def test_a_delivered_alert_keeps_its_trace(self, tmp_path):
        """The regression this file exists for.

        Two alerts are held. One delivers, one does not. Before the fix only
        the undelivered one's trace survived, so the admin who DID receive an
        alert was the one sent to an empty file.
        """
        log = _write_log(tmp_path, FLUSHED, STILL_HELD, STALE)
        entries = [_entry(FLUSHED), _entry(STILL_HELD)]

        bot = MagicMock()
        sent = []

        async def _send(chat_id, text):
            # The first alert goes out; the second fails, as it would if the
            # network were still bad.
            if STILL_HELD in text:
                raise RuntimeError("still unreachable")
            sent.append(text)

        bot.send_message = AsyncMock(side_effect=_send)

        with patch.object(er, "ERRORS_LOG", log), \
             patch.object(er, "_load_pending", lambda: list(entries)), \
             patch.object(er, "ops_chat_ids", AsyncMock(return_value={999000001})), \
             patch.object(er, "_store_pending", lambda e: entries.__setitem__(
                 slice(None), list(e))):
            held_before = er.pending_error_ids()
            await er.flush_pending_alerts(bot)
            er.reset_error_log(also_retain=held_before)

        remaining = log.read_text(encoding="utf-8")
        assert sent, "the first alert was never delivered; test proves nothing"
        assert FLUSHED in remaining, (
            "an alert was delivered saying 'Full trace: logs/errors.log "
            f"(search {FLUSHED})' and that trace was then deleted"
        )
        assert STILL_HELD in remaining, (
            "an undelivered alert lost its trace — the case that already worked"
        )
        assert STALE not in remaining, (
            "unreferenced traces from the previous run should still be cleared; "
            "otherwise errors.log grows forever"
        )

    def test_an_unreferenced_trace_is_still_cleared(self, tmp_path):
        """The guard against over-correcting into 'keep everything'."""
        log = _write_log(tmp_path, STALE)
        with patch.object(er, "ERRORS_LOG", log), \
             patch.object(er, "_load_pending", lambda: []):
            discarded = er.reset_error_log()
        assert log.read_text(encoding="utf-8") == ""
        assert discarded > 0, "nothing was reported as discarded"

    def test_retaining_nothing_matches_the_old_behaviour(self, tmp_path):
        """also_retain defaults to retaining only what is still pending, so
        every existing caller behaves exactly as before."""
        log = _write_log(tmp_path, FLUSHED, STILL_HELD)
        with patch.object(er, "ERRORS_LOG", log), \
             patch.object(er, "_load_pending", lambda: [_entry(STILL_HELD)]):
            er.reset_error_log()
        remaining = log.read_text(encoding="utf-8")
        assert STILL_HELD in remaining
        assert FLUSHED not in remaining

    def test_also_retain_unions_rather_than_replaces(self, tmp_path):
        """Passing ids must not silently drop the still-pending ones."""
        log = _write_log(tmp_path, FLUSHED, STILL_HELD)
        with patch.object(er, "ERRORS_LOG", log), \
             patch.object(er, "_load_pending", lambda: [_entry(STILL_HELD)]):
            er.reset_error_log(also_retain={FLUSHED})
        remaining = log.read_text(encoding="utf-8")
        assert FLUSHED in remaining, "the passed id was not retained"
        assert STILL_HELD in remaining, (
            "also_retain REPLACED the pending ids instead of adding to them"
        )

    def test_none_and_empty_are_accepted(self, tmp_path):
        """Called with no argument, with None, and with an empty set."""
        for arg in (None, set(), frozenset()):
            log = _write_log(tmp_path, STALE)
            with patch.object(er, "ERRORS_LOG", log), \
                 patch.object(er, "_load_pending", lambda: []):
                er.reset_error_log(also_retain=arg)
            assert log.read_text(encoding="utf-8") == "", f"failed for {arg!r}"


class TestPendingErrorIds:
    def test_it_reads_the_ids_of_held_alerts(self):
        with patch.object(er, "_load_pending",
                          lambda: [_entry(FLUSHED), _entry(STILL_HELD)]):
            assert er.pending_error_ids() == {FLUSHED, STILL_HELD}

    def test_an_empty_queue_is_an_empty_set(self):
        with patch.object(er, "_load_pending", lambda: []):
            assert er.pending_error_ids() == set()

    def test_entries_without_an_id_are_dropped(self):
        """A None id would match every trace block in _retain_traces, keeping
        the whole file — which is how "clear the log" silently stops working."""
        with patch.object(er, "_load_pending",
                          lambda: [{"text": "no id"}, {"error_id": None},
                                   {"error_id": ""}, _entry(FLUSHED)]):
            assert er.pending_error_ids() == {FLUSHED}


class TestPostInitWiresItUp:
    """The fix is only real if post_init passes the ids through.

    Mutation check: reverting post_init to a bare reset_error_log() leaves
    every test above passing and restores the bug in production.
    """

    async def test_post_init_captures_held_ids_before_flushing(self):
        import bot

        order = []
        app = MagicMock()
        app.bot.set_my_commands = AsyncMock()

        def _pending_ids():
            order.append("captured")
            return {FLUSHED}

        async def _flush(_bot):
            order.append("flushed")
            return 1

        def _reset(also_retain=None):
            order.append(("reset", set(also_retain or ())))
            return 0

        with patch.object(bot.error_reporting, "pending_error_ids", _pending_ids), \
             patch.object(bot.error_reporting, "flush_pending_alerts", _flush), \
             patch.object(bot.error_reporting, "reset_error_log", _reset), \
             patch.object(bot, "schedule_all_upcoming", AsyncMock()), \
             patch.object(bot, "alert_on_bad_service_links", AsyncMock()), \
             patch.object(bot, "_register_jobs", lambda a: None):
            await bot.post_init(app)

        assert order[0] == "captured", (
            "the held ids must be read BEFORE flushing — after the flush the "
            "delivered alerts are gone from the queue and their ids are "
            "unrecoverable, which is the bug itself"
        )
        assert order[1] == "flushed"
        assert order[2] == ("reset", {FLUSHED}), (
            f"reset_error_log was called as {order[2]}, so the trace behind "
            f"the alert just delivered is still being deleted"
        )

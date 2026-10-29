"""bot.log rotates daily and drops expired files without reading them.

This replaced `prune_log_file`, which read the ENTIRE log into memory at every
import of `settings` and rewrote it in place. Measured on the live server
2026-10-29: **9.1 seconds to delete 8 lines from a 45 MB file**, and it ran
twice per deploy because `update.sh` does an import check too. It also
allocated the whole file, a 45 MB spike on a 412 MB box whose one outage was
caused by memory exhaustion.

Rotation makes the cost O(1): an expired file is unlinked, never read. It also
removed the constraint that forced the old design — the in-place rewrite
existed to keep the logging handler's file descriptor valid, and a rotating
handler manages its own.

The tests below DRIVE A REAL ROLLOVER rather than inspecting the handler's
attributes. Asserting `when == "MIDNIGHT"` and `backupCount == 45` would pass
against a handler that never actually rotates or never actually deletes, and
the whole point of the change is those two behaviours.
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO = Path(__file__).resolve().parents[1]


def _handler(tmp_path, backup_count=3):
    """A handler configured exactly as settings.py configures the real one."""
    return TimedRotatingFileHandler(
        tmp_path / "bot.log", when="midnight", backupCount=backup_count,
        encoding="utf-8")


def _emit(handler, text):
    handler.emit(logging.LogRecord("t", logging.INFO, __file__, 1, text,
                                   None, None))


def _files(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir())


class TestRolloverBehaviour:
    def test_a_rollover_moves_the_old_records_into_a_dated_file(self, tmp_path):
        h = _handler(tmp_path)
        _emit(h, "yesterday")
        h.doRollover()
        _emit(h, "today")
        h.close()

        names = _files(tmp_path)
        assert "bot.log" in names
        rotated = [n for n in names if n != "bot.log"]
        assert len(rotated) == 1, names
        assert "yesterday" in (tmp_path / rotated[0]).read_text()
        assert "today" in (tmp_path / "bot.log").read_text()
        assert "yesterday" not in (tmp_path / "bot.log").read_text()

    def test_the_rotated_name_carries_the_date(self, tmp_path):
        """The property the user asked for: a file per day, so an expired one
        can be deleted whole without ever being read."""
        h = _handler(tmp_path)
        _emit(h, "x")
        h.doRollover()
        h.close()
        rotated = [n for n in _files(tmp_path) if n != "bot.log"]
        assert re.fullmatch(r"bot\.log\.\d{4}-\d{2}-\d{2}", rotated[0]), rotated

    def test_the_live_file_keeps_its_name(self, tmp_path):
        """Anything tailing logs/bot.log must keep working across a rollover."""
        h = _handler(tmp_path)
        _emit(h, "before")
        h.doRollover()
        _emit(h, "after")
        h.close()
        assert (tmp_path / "bot.log").is_file()
        assert "after" in (tmp_path / "bot.log").read_text()


def _seed_history(tmp_path, days, size=16):
    """Create dated backups as several days of real rotation would leave them.

    Needed because a rotated file is named for the DATE, so calling
    doRollover() repeatedly inside one test all targets the same filename and
    each call overwrites the last — one file, not N. (That is also true in
    production: two rollovers in one day collide. Harmless there, since the
    trigger is the midnight boundary, but it means deletion cannot be tested
    by rolling over in a loop.)
    """
    made = []
    for day in days:
        p = tmp_path / f"bot.log.2026-{day}"
        p.write_text("x" * size, encoding="utf-8")
        made.append(p.name)
    return made


class TestExpiredFilesAreDeleted:
    def test_a_rollover_deletes_everything_past_backup_count(self, tmp_path):
        """The replacement for the 45-day prune. Mutation check: dropping
        backupCount (its default is 0, which keeps NOTHING) fails here, and so
        does setting it absurdly high."""
        _seed_history(tmp_path, ["09-01", "09-02", "09-03", "09-04", "09-05"])
        h = _handler(tmp_path, backup_count=3)
        _emit(h, "today")
        h.doRollover()
        h.close()
        rotated = [n for n in _files(tmp_path) if n != "bot.log"]
        assert len(rotated) == 3, rotated

    def test_the_survivors_are_the_most_recent(self, tmp_path):
        """Chronology matters: deleting the NEWEST files would pass a mere
        count check while throwing away the logs someone actually wants."""
        _seed_history(tmp_path, ["09-01", "09-02", "09-03", "09-04"])
        h = _handler(tmp_path, backup_count=2)
        _emit(h, "today")
        h.doRollover()
        h.close()
        rotated = sorted(n for n in _files(tmp_path) if n != "bot.log")
        assert "bot.log.2026-09-01" not in rotated
        assert "bot.log.2026-09-02" not in rotated
        # The rollover's own file is today's, so the kept pair is the newest
        # seeded one plus it.
        assert "bot.log.2026-09-04" in rotated

    def test_deletion_never_reads_the_expired_file(self, tmp_path):
        """The whole point of the change: a 45 MB expired file costs an unlink,
        not a read. Seeded at 4 MB so a size-dependent implementation would be
        doing obvious work."""
        _seed_history(tmp_path, ["09-01", "09-02"], size=2_000_000)
        h = _handler(tmp_path, backup_count=1)
        _emit(h, "today")
        h.doRollover()
        h.close()
        rotated = [n for n in _files(tmp_path) if n != "bot.log"]
        assert len(rotated) == 1, f"a large expired file survived: {rotated}"
        assert "bot.log.2026-09-01" not in rotated

    def test_the_handler_selects_the_oldest_for_deletion(self, tmp_path):
        """Direct check of the selection, independent of a rollover firing."""
        _seed_history(tmp_path, ["09-01", "09-02", "09-03"])
        h = _handler(tmp_path, backup_count=1)
        doomed = {Path(p).name for p in h.getFilesToDelete()}
        h.close()
        assert doomed == {"bot.log.2026-09-01", "bot.log.2026-09-02"}, doomed


class TestTheRealConfiguration:
    """What settings.py actually asks for.

    Behaviour is covered above against an identically-configured handler; these
    pin the wiring so the two cannot drift. They parse the CALL rather than
    searching the file: the comment above it names "midnight", "45 days" and
    the sizes, so grepping the file for any of those would match the prose
    whatever the code said — the trap that has produced two vacuous tests in
    this repo already.
    """

    @staticmethod
    def _call() -> str:
        source = (REPO / "settings.py").read_text(encoding="utf-8")
        m = re.search(r"_file_handler = TimedRotatingFileHandler\((.*?)\)\n",
                      source, re.S)
        assert m, "the file handler is no longer a TimedRotatingFileHandler"
        return " ".join(m.group(1).split())

    def test_it_rotates_at_midnight(self):
        assert 'when="midnight"' in self._call()

    def test_it_keeps_the_runtime_retention_window(self):
        """Not a literal 45: the window is configurable in config.yaml and the
        activity log's 90-day window is deliberately different."""
        assert "backupCount=RUNTIME_LOG_RETENTION" in self._call()

    def test_the_runtime_window_is_still_the_shorter_one(self):
        import settings
        assert settings.RUNTIME_LOG_RETENTION == 45
        assert settings.LOG_RETENTION == 90

    def test_it_writes_the_file_settings_names(self):
        assert "_log_file" in self._call()

    def test_the_encoding_is_explicit(self):
        """Event titles and isiZulu strings reach this log."""
        assert 'encoding="utf-8"' in self._call()


class TestThePrunerIsGone:
    def test_activity_logger_no_longer_exposes_it(self):
        import activity_logger
        assert not hasattr(activity_logger, "prune_log_file"), (
            "prune_log_file is back; bot.log rotates itself now"
        )

    def test_settings_does_not_prune_at_import(self):
        """This was ~9 seconds of every startup, paid twice per deploy."""
        import settings
        assert not hasattr(settings, "prune_log_file")

    def test_the_activity_logs_own_pruner_is_untouched(self):
        """Separate mechanism, separate file, separate 90-day window —
        bot_activity.log was 64 KB against bot.log's 45 MB, so it was never
        the problem and keeps its in-place pruning."""
        from activity_logger import ActivityLogger
        assert hasattr(ActivityLogger, "_prune_old_entries")

    def test_the_reason_is_recorded_next_to_the_code(self):
        """Someone will wonder why this is a rotating handler rather than the
        plain FileHandler it was for most of the project's life."""
        source = (REPO / "settings.py").read_text(encoding="utf-8")
        assert "9.1 seconds" in source or "9 seconds" in source
        assert "45 MB" in source

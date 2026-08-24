"""Tests for activity_logger.py."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))
from activity_logger import ActivityLogger

TZ = pytz.timezone("America/New_York")


class TestActivityLogger:
    def setup_method(self, tmp_path_factory=None):
        pass  # each test creates its own tmp_path

    def _logger(self, tmp_path: Path) -> ActivityLogger:
        return ActivityLogger(tmp_path, retention_days=180, tz=TZ)

    def _log_file(self, tmp_path: Path) -> Path:
        return tmp_path / "bot_activity.log"

    # ------------------------------------------------------------------
    # Basic write tests
    # ------------------------------------------------------------------

    def test_creates_log_file(self, tmp_path):
        lg = self._logger(tmp_path)
        lg.log_command("start", 123, "testuser", "Test User")
        assert self._log_file(tmp_path).exists()

    def test_log_command_writes_command(self, tmp_path):
        lg = self._logger(tmp_path)
        lg.log_command("start", 123, "alice", "Alice")
        content = self._log_file(tmp_path).read_text()
        assert "COMMAND" in content
        assert "/start" in content

    def test_log_command_includes_username(self, tmp_path):
        lg = self._logger(tmp_path)
        lg.log_command("help", 456, "bob", "Bob Smith")
        content = self._log_file(tmp_path).read_text()
        assert "@bob" in content

    def test_log_command_includes_display_name(self, tmp_path):
        lg = self._logger(tmp_path)
        lg.log_command("events", 789, None, "Carol Jones")
        content = self._log_file(tmp_path).read_text()
        assert "Carol Jones" in content

    def test_log_command_includes_details(self, tmp_path):
        lg = self._logger(tmp_path)
        lg.log_command("addevent", 1, "admin", "Admin", details="Added 'Revival Night'")
        content = self._log_file(tmp_path).read_text()
        assert "Revival Night" in content

    def test_log_user_joined(self, tmp_path):
        lg = self._logger(tmp_path)
        lg.log_user_joined(111, "newuser", "New User")
        content = self._log_file(tmp_path).read_text()
        assert "USER_JOINED" in content
        assert "@newuser" in content

    def test_log_user_left(self, tmp_path):
        lg = self._logger(tmp_path)
        lg.log_user_left(222, "olduser", "Old User")
        content = self._log_file(tmp_path).read_text()
        assert "USER_LEFT" in content

    def test_log_notification_sent(self, tmp_path):
        lg = self._logger(tmp_path)
        lg.log_notification_sent("Sabbath Eve", 42)
        content = self._log_file(tmp_path).read_text()
        assert "NOTIFICATION" in content
        assert "Sabbath Eve" in content
        assert "42" in content

    def test_log_error(self, tmp_path):
        lg = self._logger(tmp_path)
        lg.log_error("Something went wrong")
        content = self._log_file(tmp_path).read_text()
        assert "ERROR" in content
        assert "Something went wrong" in content

    def test_multiple_entries_appended(self, tmp_path):
        lg = self._logger(tmp_path)
        lg.log_command("start", 1, "a", "A")
        lg.log_command("help", 2, "b", "B")
        lg.log_command("events", 3, "c", "C")
        lines = [ln for ln in self._log_file(tmp_path).read_text().splitlines() if ln.strip()]
        assert len(lines) == 3

    def test_log_entry_has_timestamp(self, tmp_path):
        lg = self._logger(tmp_path)
        lg.log_command("start", 1, "u", "U")
        content = self._log_file(tmp_path).read_text()
        # Timestamp format: [YYYY-MM-DD HH:MM:SS TZ]
        import re
        assert re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", content)

    def test_system_event_shows_system_as_who(self, tmp_path):
        lg = self._logger(tmp_path)
        lg.log_notification_sent("Test Event", 5)
        content = self._log_file(tmp_path).read_text()
        assert "system" in content

    def test_no_username_shows_only_display_name(self, tmp_path):
        lg = self._logger(tmp_path)
        lg.log_command("start", 99, None, "Anonymous User")
        content = self._log_file(tmp_path).read_text()
        assert "Anonymous User" in content
        assert "@ " not in content  # no stray "@" from missing username

    # ------------------------------------------------------------------
    # Retention / pruning
    # ------------------------------------------------------------------

    def test_old_entries_pruned(self, tmp_path):
        self._logger(tmp_path)
        log_file = self._log_file(tmp_path)
        # Manually write a very old entry
        old_date = (datetime.now(TZ) - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S %Z")
        log_file.write_text(f"[{old_date}] [COMMAND] user | /start\n")
        # Re-instantiate logger to trigger pruning
        ActivityLogger(tmp_path, retention_days=180, tz=TZ)
        content = log_file.read_text()
        assert "/start" not in content

    def test_recent_entries_kept(self, tmp_path):
        self._logger(tmp_path)
        log_file = self._log_file(tmp_path)
        recent_date = (datetime.now(TZ) - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S %Z")
        log_file.write_text(f"[{recent_date}] [COMMAND] user | /events\n")
        ActivityLogger(tmp_path, retention_days=180, tz=TZ)
        content = log_file.read_text()
        assert "/events" in content


class TestRetentionDefault:
    def test_default_retention_is_90_days(self, tmp_path):
        """Activity records are kept 3 months (trimmed from 6 for disk on a
        small always-on server)."""
        assert ActivityLogger(tmp_path, tz=TZ).retention_days == 90

    def test_config_sets_ninety_days(self):
        import settings
        assert settings.LOG_RETENTION == 90


# ---------------------------------------------------------------------------
# Runtime-log pruning (bot.log)
# ---------------------------------------------------------------------------

class TestPruneLogFile:
    """bot.log holds multi-line tracebacks and is kept open by a live
    FileHandler, so pruning must group records and rewrite in place."""

    def _write(self, tmp_path, text):
        p = tmp_path / "bot.log"
        p.write_text(text, encoding="utf-8")
        return p

    def _line(self, when, msg="[INFO] bot: hello"):
        return f"{when.strftime('%Y-%m-%d %H:%M:%S')},123 {msg}\n"

    def test_old_records_removed(self, tmp_path):
        from activity_logger import prune_log_file
        now = datetime.now(TZ)
        p = self._write(tmp_path, self._line(now - timedelta(days=200))
                        + self._line(now - timedelta(days=1)))
        removed = prune_log_file(p, 90, TZ, now)
        assert removed == 1
        assert "200" not in p.read_text()
        assert len(p.read_text().splitlines()) == 1

    def test_recent_records_kept(self, tmp_path):
        from activity_logger import prune_log_file
        now = datetime.now(TZ)
        p = self._write(tmp_path, self._line(now - timedelta(days=10)))
        assert prune_log_file(p, 90, TZ, now) == 0
        assert p.read_text().strip() != ""

    def test_multiline_traceback_kept_with_its_record(self, tmp_path):
        from activity_logger import prune_log_file
        now = datetime.now(TZ)
        recent_trace = (self._line(now - timedelta(days=2), "[ERROR] bot: boom")
                        + "Traceback (most recent call last):\n"
                        + '  File "bot.py", line 12, in handler\n'
                        + "ValueError: boom\n")
        p = self._write(tmp_path, recent_trace)
        assert prune_log_file(p, 90, TZ, now) == 0
        assert "ValueError: boom" in p.read_text()

    def test_old_traceback_removed_entirely(self, tmp_path):
        from activity_logger import prune_log_file
        now = datetime.now(TZ)
        old_trace = (self._line(now - timedelta(days=120), "[ERROR] bot: ancient")
                     + "Traceback (most recent call last):\n"
                     + "KeyError: gone\n")
        p = self._write(tmp_path, old_trace + self._line(now, "[INFO] bot: current"))
        prune_log_file(p, 90, TZ, now)
        text = p.read_text()
        assert "KeyError: gone" not in text, "orphaned continuation lines must go too"
        assert "Traceback" not in text
        assert "current" in text

    def test_unparseable_timestamps_are_kept(self, tmp_path):
        from activity_logger import prune_log_file
        now = datetime.now(TZ)
        p = self._write(tmp_path, "9999-99-99 99:99:99 [INFO] weird\n")
        assert prune_log_file(p, 90, TZ, now) == 0

    def test_missing_file_is_noop(self, tmp_path):
        from activity_logger import prune_log_file
        assert prune_log_file(tmp_path / "absent.log", 90, TZ) == 0

    def test_rewrites_in_place_preserving_inode(self, tmp_path):
        """The live FileHandler holds this fd; replacing the file would send
        later writes to an unlinked inode."""
        from activity_logger import prune_log_file
        now = datetime.now(TZ)
        p = self._write(tmp_path, self._line(now - timedelta(days=200))
                        + self._line(now))
        before = p.stat().st_ino
        prune_log_file(p, 90, TZ, now)
        assert p.stat().st_ino == before

    def test_appends_after_prune_are_retained(self, tmp_path):
        """Simulate the handler writing after a prune."""
        from activity_logger import prune_log_file
        now = datetime.now(TZ)
        p = self._write(tmp_path, self._line(now - timedelta(days=200)))
        with open(p, "a", encoding="utf-8") as handler_fd:
            prune_log_file(p, 90, TZ, now)
            handler_fd.write(self._line(now, "[INFO] bot: after prune"))
        assert "after prune" in p.read_text()

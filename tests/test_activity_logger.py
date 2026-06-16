"""Tests for activity_logger.py."""

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytz
import pytest

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
        lines = [l for l in self._log_file(tmp_path).read_text().splitlines() if l.strip()]
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
        lg = self._logger(tmp_path)
        log_file = self._log_file(tmp_path)
        # Manually write a very old entry
        old_date = (datetime.now(TZ) - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S %Z")
        log_file.write_text(f"[{old_date}] [COMMAND] user | /start\n")
        # Re-instantiate logger to trigger pruning
        lg2 = ActivityLogger(tmp_path, retention_days=180, tz=TZ)
        content = log_file.read_text()
        assert "/start" not in content

    def test_recent_entries_kept(self, tmp_path):
        lg = self._logger(tmp_path)
        log_file = self._log_file(tmp_path)
        recent_date = (datetime.now(TZ) - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S %Z")
        log_file.write_text(f"[{recent_date}] [COMMAND] user | /events\n")
        lg2 = ActivityLogger(tmp_path, retention_days=180, tz=TZ)
        content = log_file.read_text()
        assert "/events" in content

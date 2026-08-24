"""Activity logger — writes human-readable log entries to a rolling text file.

Records are kept for 3 months (configurable via retention_days).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytz

_log = logging.getLogger(__name__)


class ActivityLogger:
    def __init__(self, logs_dir: str | Path, retention_days: int = 90,
                 tz: pytz.BaseTzInfo | None = None) -> None:
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.tz = tz or pytz.utc
        self._log_file = self.logs_dir / "bot_activity.log"
        self._prune_old_entries()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_command(
        self,
        command: str,
        user_id: int | None,
        username: str | None,
        display_name: str | None,
        details: str = "",
    ) -> None:
        self._write(
            event_type="COMMAND",
            user_id=user_id,
            username=username,
            display_name=display_name,
            detail=f"/{command}" + (f" — {details}" if details else ""),
        )

    def log_user_joined(
        self, user_id: int | None, username: str | None, display_name: str | None
    ) -> None:
        self._write(
            event_type="USER_JOINED",
            user_id=user_id,
            username=username,
            display_name=display_name,
            detail="User started the bot",
        )

    def log_user_left(
        self, user_id: int | None, username: str | None, display_name: str | None
    ) -> None:
        self._write(
            event_type="USER_LEFT",
            user_id=user_id,
            username=username,
            display_name=display_name,
            detail="User stopped / blocked the bot",
        )

    def log_notification_sent(self, event_name: str, recipient_count: int) -> None:
        self._write(
            event_type="NOTIFICATION",
            user_id=None,
            username=None,
            display_name=None,
            detail=f"Sent notification for '{event_name}' to {recipient_count} user(s)",
        )

    def log_error(self, description: str) -> None:
        self._write(
            event_type="ERROR",
            user_id=None,
            username=None,
            display_name=None,
            detail=description,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write(
        self,
        event_type: str,
        user_id: int | None,
        username: str | None,
        display_name: str | None,
        detail: str,
    ) -> None:
        now = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M:%S %Z")
        who_parts: list[str] = []
        if display_name:
            who_parts.append(display_name)
        if username:
            who_parts.append(f"@{username}")
        if user_id:
            who_parts.append(f"(id:{user_id})")
        who = " ".join(who_parts) if who_parts else "system"
        line = f"[{now}] [{event_type}] {who} | {detail}\n"
        try:
            with open(self._log_file, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            _log.error("Failed to write activity log: %s", exc)

    def _prune_old_entries(self) -> None:
        """Remove log lines older than retention_days."""
        if not self._log_file.exists():
            return
        cutoff = datetime.now(self.tz) - timedelta(days=self.retention_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        try:
            lines = self._log_file.read_text(encoding="utf-8").splitlines(keepends=True)
            kept = [ln for ln in lines if ln[1:11] >= cutoff_str]
            self._log_file.write_text("".join(kept), encoding="utf-8")
        except OSError as exc:
            _log.error("Failed to prune log: %s", exc)


# ---------------------------------------------------------------------------
# Runtime-log pruning (bot.log)
# ---------------------------------------------------------------------------

# A record starts with "YYYY-MM-DD HH:MM:SS"; continuation lines (tracebacks)
# have no timestamp and belong to the record above them.
_RECORD_START = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def prune_log_file(path: "str | Path", retention_days: int,
                   tz: pytz.BaseTzInfo, now: "datetime | None" = None) -> int:
    """Drop log records older than *retention_days* from *path*.

    Written for bot.log, which the logging FileHandler keeps open: the file is
    rewritten IN PLACE (seek/truncate on the same inode) rather than replaced,
    so the handler's descriptor stays valid and later writes are not lost to an
    unlinked file.

    Multi-line records are kept intact — a traceback's continuation lines
    inherit the keep/drop decision of the timestamped line that started it.
    Returns the number of lines removed.
    """
    path = Path(path)
    if not path.exists():
        return 0
    cutoff = (now or datetime.now(tz)) - timedelta(days=retention_days)
    try:
        with open(path, "r+", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
            kept: list[str] = []
            keeping = True          # keep anything before the first timestamp
            for line in lines:
                m = _RECORD_START.match(line)
                if m:
                    try:
                        stamp = tz.localize(
                            datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                        keeping = stamp >= cutoff
                    except (ValueError, TypeError):
                        keeping = True      # unparseable date: keep it
                if keeping:
                    kept.append(line)
            removed = len(lines) - len(kept)
            if removed:
                fh.seek(0)
                fh.writelines(kept)
                fh.truncate()
            return removed
    except OSError as exc:
        _log.error("Failed to prune %s: %s", path, exc)
        return 0

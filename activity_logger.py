"""Activity logger — writes human-readable log entries to a rolling text file.

Records are kept for 6 months (configurable via retention_days).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytz

_log = logging.getLogger(__name__)


class ActivityLogger:
    def __init__(self, logs_dir: str | Path, retention_days: int = 180, tz: pytz.BaseTzInfo | None = None) -> None:
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

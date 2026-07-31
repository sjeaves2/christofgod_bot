"""File-backed in-memory cache.

Checks the file's mtime on every access (throttled to once per 5 s) and
reloads if the file was modified within the last 60 seconds, so manual edits
to YAML files are picked up automatically.

Two serialization modes:
  - default: PyYAML safe_load/dump — for machine-owned files.
  - round_trip=True: ruamel.yaml round-trip mode — preserves comments,
    quoting, and indentation across load/save, for files admins also edit
    by hand (e.g. events.yaml).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML


def _make_rt_yaml() -> YAML:
    rt = YAML(typ="rt")  # round-trip: keeps comments and formatting
    rt.preserve_quotes = True
    rt.indent(mapping=2, sequence=4, offset=2)
    rt.allow_unicode = True
    rt.width = 4096  # don't wrap long lines (e.g. Zoom URLs)
    return rt


class FileCache:
    """Single-file YAML cache with mtime-based hot-reload."""

    _MTIME_CHECK_INTERVAL = 5  # seconds between stat() calls
    _RELOAD_WINDOW = 60        # reload if file was modified within this many seconds

    def __init__(self, filepath: str | Path, round_trip: bool = False) -> None:
        self.filepath = Path(filepath)
        self._data: Any = None
        self._last_mtime: float = 0.0
        self._last_check: float = 0.0
        self._lock = asyncio.Lock()
        self._rt = _make_rt_yaml() if round_trip else None

    async def get(self) -> Any:
        async with self._lock:
            now = time.time()
            if now - self._last_check >= self._MTIME_CHECK_INTERVAL:
                self._last_check = now
                try:
                    mtime = self.filepath.stat().st_mtime
                    # Reload if file changed AND was modified recently
                    if mtime != self._last_mtime and (now - mtime) < self._RELOAD_WINDOW:
                        self._reload()
                        self._last_mtime = mtime
                    elif self._data is None:
                        self._reload()
                        self._last_mtime = mtime
                except FileNotFoundError:
                    self._data = {}
            return self._data

    def get_sync(self) -> Any:
        """Synchronous get — for use outside async context (e.g. startup)."""
        now = time.time()
        try:
            mtime = self.filepath.stat().st_mtime
            if mtime != self._last_mtime or self._data is None:
                self._reload()
                self._last_mtime = mtime
        except FileNotFoundError:
            self._data = {}
        self._last_check = now
        return self._data

    def _reload(self) -> None:
        try:
            with open(self.filepath, encoding="utf-8") as fh:
                if self._rt is not None:
                    self._data = self._rt.load(fh) or {}
                else:
                    self._data = yaml.safe_load(fh) or {}
        except Exception:
            self._data = {}

    def _dump(self, data: Any, fh) -> None:
        if self._rt is not None:
            self._rt.dump(data, fh)
        else:
            yaml.dump(data, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)

    async def save(self, data: Any) -> None:
        async with self._lock:
            self._data = data
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(self.filepath, "w", encoding="utf-8") as fh:
                self._dump(data, fh)
            try:
                self._last_mtime = self.filepath.stat().st_mtime
            except FileNotFoundError:
                pass
            self._last_check = time.time()

    def save_sync(self, data: Any) -> None:
        self._data = data
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(self.filepath, "w", encoding="utf-8") as fh:
            self._dump(data, fh)
        try:
            self._last_mtime = self.filepath.stat().st_mtime
        except FileNotFoundError:
            pass
        self._last_check = time.time()

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


def make_rt_yaml() -> YAML:
    """A ruamel round-trip YAML configured for this project's files.

    Shared with permissions.py so every hand-edited YAML file (events.yaml,
    officials.yaml) keeps its comments and formatting across bot writes.
    """
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
        self._rt = make_rt_yaml() if round_trip else None

    async def get(self) -> Any:
        async with self._lock:
            return self._refresh_locked()

    def _refresh_locked(self) -> Any:
        """The mtime check and conditional reload. **Caller must hold the lock.**

        Split out of `get()` so `save_key` can refresh and write under ONE
        lock acquisition. `asyncio.Lock` is not reentrant, so calling `get()`
        from inside a locked block would deadlock the bot rather than fail —
        hence a separate helper rather than a flag on `get()`.
        """
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
        elif self._data is None:
            # A cold cache inside the throttle window. `get()` could leave
            # `_data` as None here, which was harmless for a reader (it gets
            # None and copes) but is NOT harmless for save_key: a None would
            # start a fresh document and drop the file's other keys, which is
            # the whole bug. Load it.
            try:
                self._last_mtime = self.filepath.stat().st_mtime
                self._reload()
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

    async def save_key(self, key: str, value: Any) -> None:
        """Replace one top-level key, leaving the rest of the document alone.

        Five callers in storage.py used to do this by hand, reaching into
        `self._data` from outside the class; moving it here was the pure part
        of that change. The logic below is NOT unchanged any more — an earlier
        version of this docstring said so, and v0.28.14 changed it.

        Refreshes before merging, so a save with no preceding read keeps the
        file's other top-level keys. **Until v0.28.14 it did not:** it read
        `self._data`, which is None on a cold cache, so `_data or {}` started a
        FRESH document and silently dropped every other key. Nothing had ever
        hit it — all five callers in storage.py read before writing, and each
        of those five files happens to hold exactly one top-level key — but
        both of those are circumstances, not guarantees, and the failure mode
        was silent data loss from a function named "save one key".

        The refresh and the write happen under ONE lock acquisition, which is
        the reason for `_refresh_locked`. The simpler `await self.get()` then
        `await self.save(...)` fixes the dropped keys just as well and is
        **not** a live bug today: an uncontended `asyncio.Lock` acquire does
        not yield, and nothing else between those two lines does either, so no
        other task can interleave. Verified, not assumed — the claim that it
        races was wrong when first written here.

        Single acquisition is still the right shape, for a narrower reason:
        read-modify-write is atomic BY CONSTRUCTION rather than by the
        accident that no await currently sits between the two halves. The day
        anything in that gap gains an await — async file I/O, a metric, a log
        flush — the two-call version silently becomes a real race, and nothing
        would fail. Since that difference is invisible to behavioural tests,
        it is pinned structurally in test_cache.py instead.

        It does mean the write path now sees on-disk changes it previously
        could not: an external edit within `_RELOAD_WINDOW` is merged instead
        of being clobbered by a stale cache. That is the intended direction —
        these YAML files are hand-edited by admins — but it IS a behaviour
        change, which is why it waited for a deliberate decision rather than
        riding along with something else.
        """
        async with self._lock:
            data = self._refresh_locked() or {}
            data[key] = value
            self._write_locked(data)

    async def save(self, data: Any) -> None:
        async with self._lock:
            self._write_locked(data)

    def _write_locked(self, data: Any) -> None:
        """Replace the whole document. **Caller must hold the lock.**

        Shared by `save` and `save_key` so there is one write path, not two
        that can drift. `save_sync` keeps its own copy deliberately: it is the
        startup path and takes no lock, because there is no event loop to hold
        one on.
        """
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

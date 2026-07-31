"""Tests for cache.py — file-backed in-memory cache."""

import asyncio
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from cache import FileCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_yaml(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f)


# ---------------------------------------------------------------------------
# Sync API
# ---------------------------------------------------------------------------

class TestFileCacheSync:
    def test_returns_empty_dict_for_missing_file(self, tmp_path):
        cache = FileCache(tmp_path / "missing.yaml")
        assert cache.get_sync() == {}

    def test_loads_yaml_file(self, tmp_path):
        p = tmp_path / "data.yaml"
        write_yaml(p, {"key": "value"})
        cache = FileCache(p)
        assert cache.get_sync() == {"key": "value"}

    def test_save_sync_persists_to_disk(self, tmp_path):
        p = tmp_path / "data.yaml"
        cache = FileCache(p)
        cache.save_sync({"foo": "bar"})
        loaded = yaml.safe_load(p.read_text())
        assert loaded == {"foo": "bar"}

    def test_save_sync_updates_in_memory(self, tmp_path):
        p = tmp_path / "data.yaml"
        cache = FileCache(p)
        cache.save_sync({"x": 1})
        assert cache._data == {"x": 1}

    def test_get_sync_returns_cached_when_file_unchanged(self, tmp_path):
        p = tmp_path / "data.yaml"
        write_yaml(p, {"a": 1})
        cache = FileCache(p)
        cache.get_sync()  # prime the in-memory cache
        # Mutate in-memory without touching file
        cache._data["injected"] = True
        second = cache.get_sync()
        # Should still return mutated in-memory (mtime unchanged, check throttled)
        assert second.get("injected") is True

    def test_creates_parent_directory(self, tmp_path):
        p = tmp_path / "subdir" / "nested.yaml"
        cache = FileCache(p)
        cache.save_sync({"nested": True})
        assert p.exists()

    def test_handles_empty_yaml_file(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        cache = FileCache(p)
        assert cache.get_sync() == {}

    def test_hot_reload_when_recently_modified(self, tmp_path):
        p = tmp_path / "data.yaml"
        write_yaml(p, {"version": 1})
        cache = FileCache(p)
        cache.get_sync()  # prime

        # Force mtime check by resetting last_check
        cache._last_check = 0
        # Write new content
        write_yaml(p, {"version": 2})
        # Touch mtime to "now" (within 60s window)
        p.touch()

        result = cache.get_sync()
        assert result.get("version") == 2

    def test_no_reload_when_mtime_unchanged(self, tmp_path):
        p = tmp_path / "data.yaml"
        write_yaml(p, {"v": 1})
        cache = FileCache(p)
        cache.get_sync()
        # Modify in-memory, don't change file
        cache._data["injected"] = 99
        cache._last_check = 0
        # mtime hasn't changed, so no reload should happen
        cache.get_sync()
        assert cache._data.get("injected") == 99


# ---------------------------------------------------------------------------
# Async API
# ---------------------------------------------------------------------------

class TestFileCacheAsync:
    def test_async_get_returns_empty_for_missing_file(self, tmp_path):
        cache = FileCache(tmp_path / "nope.yaml")
        result = asyncio.get_event_loop().run_until_complete(cache.get())
        assert result == {}

    def test_async_get_loads_file(self, tmp_path):
        p = tmp_path / "data.yaml"
        write_yaml(p, {"hello": "world"})
        cache = FileCache(p)
        result = asyncio.get_event_loop().run_until_complete(cache.get())
        assert result == {"hello": "world"}

    def test_async_save_persists(self, tmp_path):
        p = tmp_path / "data.yaml"
        cache = FileCache(p)

        async def run():
            await cache.save({"async": True})

        asyncio.get_event_loop().run_until_complete(run())
        assert yaml.safe_load(p.read_text()) == {"async": True}

    def test_async_get_after_save_returns_new_data(self, tmp_path):
        p = tmp_path / "data.yaml"
        cache = FileCache(p)

        async def run():
            await cache.save({"n": 42})
            return await cache.get()

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result["n"] == 42


# ---------------------------------------------------------------------------
# Round-trip mode (comment/format preservation for hand-edited files)
# ---------------------------------------------------------------------------

_COMMENTED = """\
# Header comment explaining the file.
targets:
  main: -100123
# Trailing note about targets.
urls:
  sabbath::Eve: https://example.org/eve
settings:
  - alpha
  # inline list comment
  - beta
"""


class TestRoundTripMode:
    def _cache(self, tmp_path):
        p = tmp_path / "events.yaml"
        p.write_text(_COMMENTED)
        return p, FileCache(p, round_trip=True)

    def test_load_save_preserves_comments(self, tmp_path):
        p, cache = self._cache(tmp_path)
        data = cache.get_sync()
        cache.save_sync(data)
        text = p.read_text()
        assert "# Header comment explaining the file." in text
        assert "# Trailing note about targets." in text
        assert "# inline list comment" in text

    def test_edit_preserved_alongside_comments(self, tmp_path):
        p, cache = self._cache(tmp_path)
        data = cache.get_sync()
        data["urls"]["sabbath::Eve"] = "https://example.org/CHANGED"
        data["urls"]["yom_kippur::Eve"] = "https://example.org/new"
        cache.save_sync(data)
        text = p.read_text()
        assert "https://example.org/CHANGED" in text
        assert "yom_kippur::Eve" in text
        assert "# Header comment explaining the file." in text

    def test_output_remains_pyyaml_parseable(self, tmp_path):
        p, cache = self._cache(tmp_path)
        data = cache.get_sync()
        data["urls"]["x"] = "y"
        cache.save_sync(data)
        parsed = yaml.safe_load(p.read_text())
        assert parsed["urls"]["x"] == "y"
        assert parsed["targets"]["main"] == -100123

    def test_sequence_indentation_preserved(self, tmp_path):
        p, cache = self._cache(tmp_path)
        cache.save_sync(cache.get_sync())
        assert "  - alpha" in p.read_text()

    def test_default_mode_still_plain_yaml(self, tmp_path):
        p = tmp_path / "plain.yaml"
        cache = FileCache(p)
        cache.save_sync({"a": 1})
        assert yaml.safe_load(p.read_text()) == {"a": 1}

    def test_missing_file_round_trip(self, tmp_path):
        cache = FileCache(tmp_path / "missing.yaml", round_trip=True)
        assert cache.get_sync() == {}

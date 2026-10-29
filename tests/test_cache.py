"""Tests for cache.py — file-backed in-memory cache."""

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
    async def test_async_get_returns_empty_for_missing_file(self, tmp_path):
        cache = FileCache(tmp_path / "nope.yaml")
        result = await cache.get()
        assert result == {}

    async def test_async_get_loads_file(self, tmp_path):
        p = tmp_path / "data.yaml"
        write_yaml(p, {"hello": "world"})
        cache = FileCache(p)
        result = await cache.get()
        assert result == {"hello": "world"}

    async def test_async_save_persists(self, tmp_path):
        p = tmp_path / "data.yaml"
        cache = FileCache(p)
        await cache.save({"async": True})
        assert yaml.safe_load(p.read_text()) == {"async": True}

    async def test_async_get_after_save_returns_new_data(self, tmp_path):
        p = tmp_path / "data.yaml"
        cache = FileCache(p)
        await cache.save({"n": 42})
        result = await cache.get()
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


class TestSaveKey:
    """FileCache.save_key — replaces one top-level key, keeps the rest.

    Five functions in storage.py used to do this by hand, reaching into the
    private `_data` from outside the class. Moving it here changed no
    behaviour; these tests pin the behaviour so that stays true.
    """

    async def test_it_replaces_the_named_key(self, tmp_path):
        cache = FileCache(tmp_path / "s.yaml")
        await cache.save({"users": [{"id": 1}]})
        await cache.save_key("users", [{"id": 2}])
        assert (await cache.get())["users"] == [{"id": 2}]

    async def test_it_leaves_other_top_level_keys_alone(self, tmp_path):
        """The reason it merges rather than writing a fresh document: these
        files can hold more than the one list being replaced."""
        cache = FileCache(tmp_path / "s.yaml")
        await cache.save({"users": [], "schema_version": 3, "note": "keep me"})
        await cache.save_key("users", [{"id": 1}])
        data = await cache.get()
        assert data["schema_version"] == 3
        assert data["note"] == "keep me"

    async def test_it_persists_to_disk(self, tmp_path):
        import yaml
        p = tmp_path / "s.yaml"
        cache = FileCache(p)
        await cache.save({"items": []})
        await cache.save_key("items", [{"a": 1}])
        assert yaml.safe_load(p.read_text())["items"] == [{"a": 1}]

    async def test_it_adds_a_key_that_was_not_there(self, tmp_path):
        cache = FileCache(tmp_path / "s.yaml")
        await cache.save({"other": 1})
        await cache.save_key("fresh", [1, 2])
        data = await cache.get()
        assert data == {"other": 1, "fresh": [1, 2]}

    async def test_a_save_with_no_preceding_read_starts_a_fresh_document(
            self, tmp_path):
        """The documented LIMITATION, pinned so it is a known quantity rather
        than a surprise.

        `_data` is None until something loads the file, so an unread file's
        other keys are lost. Latent, not live: every caller in storage.py reads
        before writing. Fixing it means awaiting get() first, which makes the
        write path reload from disk — a behaviour change that breaks tests
        relying on the cached copy, so it is deliberately NOT done here.
        """
        import yaml
        p = tmp_path / "s.yaml"
        p.write_text(yaml.safe_dump({"users": [], "keep": "this"}),
                     encoding="utf-8")
        cache = FileCache(p)          # nothing has read it yet
        await cache.save_key("users", [{"id": 1}])
        assert "keep" not in await cache.get(), (
            "save_key now preserves keys across an unread file — if that was "
            "done deliberately, this test should be inverted, not deleted"
        )


class TestStorageUsesLateBinding:
    """storage's save functions must look their cache up at CALL time.

    tests/test_prayer.py (and others) swap in a temp-file cache with
    `storage.prayer_cache = FileCache(tmp_path / ...)`. That only works while
    the accessors resolve the module global when called.

    A ListStore holding the cache object was tried and reverted for exactly
    this reason: capturing the cache at import time silently sent writes to the
    real data/ file and broke 12 tests. The "duplication" in storage.py is
    load-bearing indirection, not accidental repetition.
    """

    async def test_rebinding_the_module_global_redirects_writes(self, tmp_path):
        import storage
        original = storage.prayer_cache
        try:
            storage.prayer_cache = FileCache(tmp_path / "prayer.yaml")
            await storage.save_prayer_requests([{"id": "PR-TEST"}])
            assert (tmp_path / "prayer.yaml").is_file(), (
                "the write did not reach the swapped-in cache; an accessor is "
                "capturing its cache at import time"
            )
            assert await storage.get_prayer_requests() == [{"id": "PR-TEST"}]
        finally:
            storage.prayer_cache = original

    async def test_the_real_cache_is_untouched_by_that(self, tmp_path):
        """Proof the previous test is not just writing everywhere."""
        import storage
        original = storage.prayer_cache
        try:
            storage.prayer_cache = FileCache(tmp_path / "p.yaml")
            await storage.save_prayer_requests([{"id": "PR-ISOLATED"}])
        finally:
            storage.prayer_cache = original
        assert not any(r.get("id") == "PR-ISOLATED"
                       for r in await storage.get_prayer_requests())

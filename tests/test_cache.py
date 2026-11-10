"""Tests for cache.py — file-backed in-memory cache."""

import ast
import asyncio
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from cache import FileCache

SOURCE = Path(__file__).parent.parent / "cache.py"


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

    async def test_a_save_with_no_preceding_read_keeps_the_other_keys(
            self, tmp_path):
        """Fixed in v0.28.14. This test is the INVERSION of the one that
        pinned the bug, as that test's own failure message asked for.

        Before: `_data` is None on a cold cache, so `_data or {}` started a
        fresh document and the file's other top-level keys were silently
        dropped. It never fired in production — every caller in storage.py
        reads first, and each of those files holds one top-level key — so this
        is the only test standing between the bug and a return to it.
        """
        import yaml
        p = tmp_path / "s.yaml"
        p.write_text(yaml.safe_dump({"users": [], "keep": "this"}),
                     encoding="utf-8")
        cache = FileCache(p)          # nothing has read it yet
        await cache.save_key("users", [{"id": 1}])

        data = await cache.get()
        assert data["keep"] == "this", (
            "a cold save_key dropped another top-level key — the v0.28.14 "
            "regression, and it loses data silently"
        )
        assert data["users"] == [{"id": 1}]
        assert yaml.safe_load(p.read_text(encoding="utf-8")) == {
            "users": [{"id": 1}], "keep": "this"
        }, "the merge happened in memory but not on disk"

    async def test_a_cold_save_inside_the_throttle_window_still_loads(
            self, tmp_path):
        """The branch the obvious fix misses.

        `_refresh_locked` only stats the file once per `_MTIME_CHECK_INTERVAL`.
        A cache constructed and written to within that window takes the
        throttled path, where the original code returned `_data` untouched —
        still None — and the merge would start from {} again. So "call the
        refresh helper" is not sufficient on its own; the cold-cache check in
        the throttled branch is load-bearing.

        Simulated by stamping `_last_check`, which is what a cache that was
        read moments ago looks like.
        """
        import time as _time
        import yaml
        p = tmp_path / "s.yaml"
        p.write_text(yaml.safe_dump({"users": [], "keep": "this"}),
                     encoding="utf-8")
        cache = FileCache(p)
        cache._last_check = _time.time()   # inside the throttle window
        assert cache._data is None, "fixture no longer sets up a cold cache"

        await cache.save_key("users", [{"id": 1}])
        assert (await cache.get())["keep"] == "this", (
            "the throttled branch left _data as None and the merge started "
            "from an empty document"
        )

    async def test_save_key_does_not_deadlock(self, tmp_path):
        """`asyncio.Lock` is not reentrant.

        The tempting one-line fix is `data = await self.get() or {}`, but
        save_key now holds the lock while it refreshes, so an inner `get()`
        would wait on a lock its own caller holds — and the bot would HANG
        rather than fail. A hang in the suite looks like a stuck CI job, not a
        red test, so it is given an explicit timeout here.
        """
        cache = FileCache(tmp_path / "s.yaml")
        await asyncio.wait_for(cache.save_key("users", [{"id": 1}]), timeout=5)
        assert (await cache.get())["users"] == [{"id": 1}]

    async def test_concurrent_saves_do_not_lose_each_other(self, tmp_path):
        """Both keys survive two save_keys issued together.

        Honest about its reach: this does NOT distinguish the single-lock
        implementation from an `await get()` / `await save()` pair. It was
        written believing it would, and it does not — an uncontended
        `asyncio.Lock` acquire never yields, so that pair has no suspension
        point between read and write and nothing can interleave either way.
        Both implementations pass, measured, not guessed.

        It is kept because it pins the OUTCOME that matters (no writer erases
        another's key) and would catch a future rewrite that genuinely does
        suspend mid-update. The structural difference is pinned separately in
        TestSaveKeyIsAtomicByConstruction below.
        """
        cache = FileCache(tmp_path / "s.yaml")
        await cache.save({})
        await asyncio.gather(
            cache.save_key("users", [{"id": 1}]),
            cache.save_key("items", [{"id": 2}]),
        )
        data = await cache.get()
        assert data == {"users": [{"id": 1}], "items": [{"id": 2}]}, (
            f"a concurrent save_key lost the other writer's key: {data}"
        )

    async def test_a_cold_save_to_a_missing_file_still_works(self, tmp_path):
        """The refresh must not turn a missing file into an error. First write
        of a brand-new data file goes through exactly this path."""
        p = tmp_path / "nested" / "new.yaml"
        cache = FileCache(p)
        await cache.save_key("users", [{"id": 1}])
        assert (await cache.get()) == {"users": [{"id": 1}]}
        assert p.exists()

    async def test_the_merge_sees_a_fresh_read_not_a_stale_snapshot(
            self, tmp_path):
        """A reload between read and write must not be clobbered.

        This is the narrow case the two-call version WOULD get wrong if it
        ever suspended: holding a reference to the pre-reload dict and writing
        it back afterwards. Simulated here by forcing the reload to land
        inside the update, which single acquisition makes impossible.
        """
        import yaml
        p = tmp_path / "s.yaml"
        p.write_text(yaml.safe_dump({"users": [], "note": "on disk"}),
                     encoding="utf-8")
        cache = FileCache(p)
        cache._data = {"users": [], "note": "stale cache"}
        cache._last_check = 0.0        # next access will stat and reload

        await cache.save_key("users", [{"id": 1}])
        assert (await cache.get())["note"] == "on disk", (
            "save_key wrote a stale snapshot over the reloaded document"
        )

    async def test_an_external_edit_is_merged_rather_than_clobbered(
            self, tmp_path):
        """The deliberate behaviour change, pinned so it is not mistaken for a
        bug later.

        These YAML files are hand-edited by admins. The write path now sees an
        edit made within `_RELOAD_WINDOW` instead of overwriting it from a
        stale cache. Documented in save_key's docstring as intended.
        """
        import yaml
        p = tmp_path / "s.yaml"
        cache = FileCache(p)
        await cache.save({"users": [], "note": "original"})

        # An admin edits the file by hand, just now.
        p.write_text(yaml.safe_dump({"users": [], "note": "hand edited"}),
                     encoding="utf-8")
        cache._last_check = 0.0        # let the next access stat the file

        await cache.save_key("users", [{"id": 1}])
        assert (await cache.get())["note"] == "hand edited", (
            "the admin's hand edit was clobbered by the stale cache"
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


class TestSaveKeyIsAtomicByConstruction:
    """save_key must hold the lock across the whole read-modify-write.

    Structural, deliberately. The alternative implementation —

        data = await self.get() or {}
        data[key] = value
        await self.save(data)

    behaves identically today and passes every behavioural test in this file,
    which was measured rather than assumed. The difference only becomes real
    once something in that gap yields, and then it is a silent lost write. So
    the guard has to be structural or it does not exist: a behavioural test
    here would be a test that proves nothing.

    See save_key's docstring for why single acquisition is the shape, and for
    the correction of an earlier, wrong claim that the two-call version races
    today.
    """

    @staticmethod
    def _save_key_node() -> ast.AsyncFunctionDef:
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "save_key":
                return node
        raise AssertionError("FileCache.save_key not found — renamed?")

    def test_it_takes_the_lock_exactly_once(self):
        node = self._save_key_node()
        locks = [n for n in ast.walk(node)
                 if isinstance(n, ast.AsyncWith)
                 and "self._lock" in ast.unparse(n.items[0].context_expr)]
        assert len(locks) == 1, (
            f"save_key acquires self._lock {len(locks)} times; it must hold it "
            f"once across the whole read-modify-write"
        )

    def test_it_does_not_call_the_locking_public_methods(self):
        """`get` and `save` each take the lock themselves.

        Calling either from inside save_key's locked block DEADLOCKS the bot —
        asyncio.Lock is not reentrant — and calling them instead of the block
        gives up atomicity. Both are caught here.

        The docstring is stripped before unparsing. save_key's own docstring
        DISCUSSES `await self.get()`, so unparsing the whole node matches that
        prose and fails against correct code — it did, on this test's first
        run. Same trap as the `break` search in test_appt_write_back.py: ask
        about the code, not the text that happens to sit next to it.
        """
        node = self._save_key_node()
        statements = [n for n in node.body
                      if not (isinstance(n, ast.Expr)
                              and isinstance(n.value, ast.Constant)
                              and isinstance(n.value.value, str))]
        assert statements, "save_key has no code outside its docstring"
        body = "\n".join(ast.unparse(n) for n in statements)
        for forbidden in ("await self.get()", "await self.save("):
            assert forbidden not in body, (
                f"save_key calls {forbidden} — see its docstring: inside the "
                f"lock this deadlocks, outside it the update stops being "
                f"atomic by construction"
            )

    def test_the_locked_helpers_do_not_take_the_lock_themselves(self):
        """The helpers exist to be called with the lock already held. If one
        acquires it, every caller deadlocks."""
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        for name in ("_refresh_locked", "_write_locked"):
            node = next((n for n in ast.walk(tree)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and n.name == name), None)
            assert node is not None, f"{name} is gone; update this guard with it"
            assert "self._lock" not in ast.unparse(node), (
                f"{name} acquires the lock, but its callers already hold it — "
                f"this deadlocks rather than failing"
            )

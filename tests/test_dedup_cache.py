# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests `src/library/dedup_cache.py` (DedupCache) in isolation: the round trip
of the compared_tier1/compared_tier2 tables (completeness of the pairwise
comparisons, cf. duplicate_detector.py -- the real incrementality of the
comparison phase), purge_missing on those two tables, the full purge on a
_CACHE_VERSION bump, and the persistence of the list of corrupted files
(the corrupted_files table, cf. duplicate_detector.py::_detect which replaces it
entirely at the end of a pass, and main_window.py which uses it so that
"Duplicate status…" survives an application restart)."""
import src.library.dedup_cache as dedup_cache_mod
from src.library.dedup_cache import DedupCache


class TestComparedTier1Tier2Roundtrip:
    def test_store_and_get_compared_tier1(self, tmp_path):
        cache = DedupCache(tmp_path / "dedup_cache.db")
        cache.open()
        try:
            cache.store_compared_tier1([("a.jpg", 100.0), ("b.jpg", 200.0)])
            result = cache.get_compared_tier1(["a.jpg", "b.jpg", "c.jpg"])
            assert result == {"a.jpg": 100.0, "b.jpg": 200.0}
        finally:
            cache.close()

    def test_store_and_get_compared_tier2(self, tmp_path):
        cache = DedupCache(tmp_path / "dedup_cache.db")
        cache.open()
        try:
            cache.store_compared_tier2([("a.jpg", 100.0)])
            result = cache.get_compared_tier2(["a.jpg", "b.jpg"])
            assert result == {"a.jpg": 100.0}
        finally:
            cache.close()

    def test_store_compared_tier1_overwrites_existing_mtime(self, tmp_path):
        cache = DedupCache(tmp_path / "dedup_cache.db")
        cache.open()
        try:
            cache.store_compared_tier1([("a.jpg", 100.0)])
            cache.store_compared_tier1([("a.jpg", 200.0)])
            assert cache.get_compared_tier1(["a.jpg"]) == {"a.jpg": 200.0}
        finally:
            cache.close()

    def test_get_compared_tier1_empty_paths_returns_empty_dict(self, tmp_path):
        cache = DedupCache(tmp_path / "dedup_cache.db")
        cache.open()
        try:
            assert cache.get_compared_tier1([]) == {}
        finally:
            cache.close()

    def test_store_compared_empty_rows_is_noop(self, tmp_path):
        cache = DedupCache(tmp_path / "dedup_cache.db")
        cache.open()
        try:
            cache.store_compared_tier1([])
            assert cache.get_compared_tier1(["a.jpg"]) == {}
        finally:
            cache.close()


class TestPurgeMissingCoversComparedTables:
    def test_purge_missing_removes_stale_compared_entries(self, tmp_path):
        cache = DedupCache(tmp_path / "dedup_cache.db")
        cache.open()
        try:
            cache.store_fingerprints([("a.jpg", 1.0, "0" * 16, 10, 10, b"\x00" * 8)])
            cache.store_compared_tier1([("a.jpg", 1.0), ("b.jpg", 1.0)])
            cache.store_compared_tier2([("a.jpg", 1.0), ("b.jpg", 1.0)])

            cache.purge_missing({"a.jpg"})

            assert cache.get_compared_tier1(["a.jpg", "b.jpg"]) == {"a.jpg": 1.0}
            assert cache.get_compared_tier2(["a.jpg", "b.jpg"]) == {"a.jpg": 1.0}
        finally:
            cache.close()

    def test_purge_missing_keeps_everything_when_nothing_stale(self, tmp_path):
        cache = DedupCache(tmp_path / "dedup_cache.db")
        cache.open()
        try:
            cache.store_compared_tier1([("a.jpg", 1.0)])
            cache.store_compared_tier2([("a.jpg", 1.0)])

            deleted = cache.purge_missing({"a.jpg"})

            assert deleted == 0
            assert cache.get_compared_tier1(["a.jpg"]) == {"a.jpg": 1.0}
            assert cache.get_compared_tier2(["a.jpg"]) == {"a.jpg": 1.0}
        finally:
            cache.close()

    def test_purge_missing_removes_stale_corrupted_entries(self, tmp_path):
        """A corrupted file never has a fingerprint/orb_features (the only normal
        source of `cached_paths` in purge_missing): without explicitly adding
        corrupted_files to that collection, a corrupted file removed from the
        library would never be purged from that table."""
        cache = DedupCache(tmp_path / "dedup_cache.db")
        cache.open()
        try:
            cache.replace_corrupted_paths(["a.jpg", "b.jpg"])

            cache.purge_missing({"a.jpg"})

            assert cache.get_corrupted_paths() == ["a.jpg"]
        finally:
            cache.close()


class TestCacheVersionBumpPurgesComparedTables:
    def test_version_bump_purges_compared_tier1_and_tier2(self, tmp_path, monkeypatch):
        db_path = tmp_path / "dedup_cache.db"
        cache1 = DedupCache(db_path)
        cache1.open()
        try:
            cache1.store_compared_tier1([("a.jpg", 1.0)])
            cache1.store_compared_tier2([("a.jpg", 1.0)])
        finally:
            cache1.close()

        monkeypatch.setattr(dedup_cache_mod, "_CACHE_VERSION", "unit-test-bumped-version")
        # __init__ calls _init_db(), which compares the stored version with
        # _CACHE_VERSION and purges everything if they differ.
        cache2 = DedupCache(db_path)
        cache2.open()
        try:
            assert cache2.get_compared_tier1(["a.jpg"]) == {}
            assert cache2.get_compared_tier2(["a.jpg"]) == {}
        finally:
            cache2.close()

    def test_same_version_does_not_purge(self, tmp_path):
        db_path = tmp_path / "dedup_cache.db"
        cache1 = DedupCache(db_path)
        cache1.open()
        try:
            cache1.store_compared_tier1([("a.jpg", 1.0)])
        finally:
            cache1.close()

        cache2 = DedupCache(db_path)
        cache2.open()
        try:
            assert cache2.get_compared_tier1(["a.jpg"]) == {"a.jpg": 1.0}
        finally:
            cache2.close()


class TestCorruptedFilesPersistence:
    def test_replace_then_get_roundtrip(self, tmp_path):
        cache = DedupCache(tmp_path / "dedup_cache.db")
        cache.open()
        try:
            cache.replace_corrupted_paths(["b.jpg", "a.jpg"])
            assert cache.get_corrupted_paths() == ["a.jpg", "b.jpg"]
        finally:
            cache.close()

    def test_replace_with_empty_clears_table(self, tmp_path):
        cache = DedupCache(tmp_path / "dedup_cache.db")
        cache.open()
        try:
            cache.replace_corrupted_paths(["a.jpg"])
            cache.replace_corrupted_paths([])
            assert cache.get_corrupted_paths() == []
        finally:
            cache.close()

    def test_replace_fully_overwrites_previous_set(self, tmp_path):
        """Reflects the real use (duplicate_detector.py::_detect, finally):
        self._corrupted is the complete and up-to-date state on every pass, so a
        file absent from the new call (repaired/deleted) must disappear,
        with no extra reconciliation logic."""
        cache = DedupCache(tmp_path / "dedup_cache.db")
        cache.open()
        try:
            cache.replace_corrupted_paths(["a.jpg", "b.jpg"])
            cache.replace_corrupted_paths(["b.jpg", "c.jpg"])
            assert cache.get_corrupted_paths() == ["b.jpg", "c.jpg"]
        finally:
            cache.close()

    def test_remove_corrupted_paths_removes_only_given_paths(self, tmp_path):
        cache = DedupCache(tmp_path / "dedup_cache.db")
        cache.open()
        try:
            cache.replace_corrupted_paths(["a.jpg", "b.jpg", "c.jpg"])
            cache.remove_corrupted_paths(["b.jpg"])
            assert cache.get_corrupted_paths() == ["a.jpg", "c.jpg"]
        finally:
            cache.close()

    def test_remove_corrupted_paths_empty_is_noop(self, tmp_path):
        cache = DedupCache(tmp_path / "dedup_cache.db")
        cache.open()
        try:
            cache.replace_corrupted_paths(["a.jpg"])
            cache.remove_corrupted_paths([])
            assert cache.get_corrupted_paths() == ["a.jpg"]
        finally:
            cache.close()

    def test_persists_across_reopen(self, tmp_path):
        db_path = tmp_path / "dedup_cache.db"
        cache1 = DedupCache(db_path)
        cache1.open()
        try:
            cache1.replace_corrupted_paths(["a.jpg"])
        finally:
            cache1.close()

        cache2 = DedupCache(db_path)
        cache2.open()
        try:
            assert cache2.get_corrupted_paths() == ["a.jpg"]
        finally:
            cache2.close()

# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste `src/library/dedup_cache.py` (DedupCache) en isolation : le round-trip
des tables compared_tier1/compared_tier2 (complétude des comparaisons par
paires, cf. duplicate_detector.py — vraie incrémentalité de la phase de
comparaison), purge_missing sur ces deux tables, la purge complète sur
bump de _CACHE_VERSION, et la persistance de la liste des fichiers corrompus
(table corrupted_files, cf. duplicate_detector.py::_detect qui la remplace
intégralement en fin de passage, et main_window.py qui l'utilise pour que
« État des doublons… » survive à un redémarrage de l'application)."""
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
        """Un fichier corrompu n'a jamais de fingerprint/orb_features (seule
        source normale de `cached_paths` dans purge_missing) : sans l'ajout
        explicite de corrupted_files à cette collecte, un fichier corrompu
        supprimé de la bibliothèque ne serait jamais purgé de cette table."""
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
        # __init__ appelle _init_db(), qui compare la version stockée à
        # _CACHE_VERSION et purge tout si elles diffèrent.
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
        """Reflète l'usage réel (duplicate_detector.py::_detect, finally) :
        self._corrupted est l'état complet et à jour à chaque passage, donc un
        fichier absent du nouvel appel (réparé/supprimé) doit disparaître,
        sans logique de réconciliation supplémentaire."""
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

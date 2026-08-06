# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste `src/ui/background_workers.py::_ResetWorkerThread` : régression sur
le bug 2026-07 où un reset (RESET_CLUSTERING ou RESET_FULL) suivi d'un
regroupement HDBSCAN avec le même nombre de visages non identifiés que le
dernier regroupement réussi sautait silencieusement le reclustering (cache
`clusterer._last_clustered_n` jamais invalidé), laissant les visages bloqués
avec `cluster_id=NULL` indéfiniment — découvert via `test_faces_reset_full`
(e2e)."""
import sqlite3

import pytest

from src.faces import clusterer
from src.faces.face_database import FaceDatabase, _enc
from src.ui.background_workers import _ResetWorkerThread


def _insert_emb_face(db, photo, emb, cluster_id=None) -> int:
    conn = sqlite3.connect(db._db_path)
    try:
        cur = conn.execute(
            "INSERT INTO faces (photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
            " embedding, cluster_id, ignored, pinned)"
            " VALUES (?,0,0,50,50,?,?,0,0)",
            (photo, _enc(emb), cluster_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _reset_sentinel(monkeypatch):
    monkeypatch.setattr(clusterer, "_last_clustered_n", -1)


@pytest.mark.parametrize("choice", [1, 2])  # RESET_CLUSTERING, RESET_FULL
def test_reset_worker_invalidates_clustering_cache(choice, tmp_path):
    db = FaceDatabase(db_path=tmp_path / "faces.db")
    for i in range(3):
        _insert_emb_face(db, f"a{i}.jpg", [1.0, 0.0, 0.0, i * 0.01])
        _insert_emb_face(db, f"b{i}.jpg", [0.0, 1.0, 0.0, i * 0.01])
    clusterer._last_clustered_n = 6  # simule un clustering réussi précédent (N=6)

    worker = _ResetWorkerThread(db, choice, threads_to_wait=[])
    worker.run()  # synchrone, cf. règle de test QThread de CLAUDE.md

    assert clusterer._last_clustered_n == -1


# ---------------------------------------------------------------------------
# Threads restants de background_workers.py — run() synchrone (règle CLAUDE.md)

from src.core.models import PhotoInfo
from src.library.catalog import Catalog
from src.ui import background_workers as bw
from src.ui.background_workers import (
    _CatalogLoadThread, _DupMigrationThread, _PersonsRefreshThread,
    _PhotoQueryThread, _ResuggestThread,
)


class _FakeCatalog:
    def __init__(self, photos):
        self._photos = photos

    def get_all_photos(self):
        return list(self._photos)


class TestCatalogLoadThread:
    def _photos(self, n):
        return [PhotoInfo(path=f"C:/lib/p{i}.jpg") for i in range(n)]

    def test_emits_batches_in_order(self):
        t = _CatalogLoadThread(_FakeCatalog(self._photos(5)), batch_size=2)
        batches = []
        t.batch_ready.connect(batches.append)

        t.run()

        assert [len(b) for b in batches] == [2, 2, 1]
        assert batches[0][0].path.endswith("p0.jpg")

    def test_reverse_order(self):
        t = _CatalogLoadThread(_FakeCatalog(self._photos(3)), batch_size=10,
                               reverse=True)
        batches = []
        t.batch_ready.connect(batches.append)

        t.run()

        assert [p.path[-6:] for p in batches[0]] == ["p2.jpg", "p1.jpg", "p0.jpg"]

    def test_stop_interrupts_batching(self):
        t = _CatalogLoadThread(_FakeCatalog(self._photos(6)), batch_size=2)
        batches = []

        def _first_batch(b):
            batches.append(b)
            t.stop()   # connexion directe : le break intervient avant le lot 2

        t.batch_ready.connect(_first_batch)
        t.run()

        assert len(batches) == 1


class TestPhotoQueryThread:
    def test_emits_query_result_with_context(self):
        photos = [PhotoInfo(path="C:/b.jpg"), PhotoInfo(path="C:/a.jpg")]
        t = _PhotoQueryThread(lambda: photos, "Dossier X")
        results = []
        t.photos_ready.connect(lambda p, ctx: results.append((p, ctx)))

        t.run()

        import os
        assert results[0][1] == "Dossier X"
        assert [p.path for p in results[0][0]] == [
            os.path.normpath("C:/b.jpg"), os.path.normpath("C:/a.jpg")
        ]

    def test_sorting_applied(self):
        photos = [PhotoInfo(path="C:/b.jpg"), PhotoInfo(path="C:/a.jpg")]
        t = _PhotoQueryThread(lambda: photos, "ctx",
                              sort_key_fn=lambda p: p.filename,
                              sort_reverse=False)
        results = []
        t.photos_ready.connect(lambda p, ctx: results.append(p))

        t.run()

        assert [p.filename for p in results[0]] == ["a.jpg", "b.jpg"]

    def test_query_error_emits_empty_list(self):
        def boom():
            raise RuntimeError("db morte")

        t = _PhotoQueryThread(boom, "ctx")
        results = []
        t.photos_ready.connect(lambda p, ctx: results.append((p, ctx)))

        t.run()

        assert results == [([], "ctx")]


class _FakeDedupCache:
    """Remplace DedupCache (qui pointe sinon vers le cache réel de l'utilisateur)."""
    instances = []

    def __init__(self):
        self.removed = None
        self.opened = False
        self.closed = False
        _FakeDedupCache.instances.append(self)

    def open(self):
        self.opened = True

    def remove_compared(self, paths):
        self.removed = list(paths)

    def close(self):
        self.closed = True


class _FakeMigrationCatalog:
    def __init__(self, groups):
        self._groups = groups          # {gid: [PhotoInfo]}
        self.ignored = []

    def count_duplicate_groups(self):
        return len(self._groups)

    def get_duplicate_groups(self):
        return dict(self._groups)

    def ignore_duplicate_group(self, gid):
        self.ignored.append(gid)
        self._groups.pop(gid, None)


class TestDupMigrationThread:
    def test_conflicting_dates_dissolved_and_uncompared(self, monkeypatch):
        from datetime import datetime
        _FakeDedupCache.instances = []
        monkeypatch.setattr(bw, "DedupCache", _FakeDedupCache)
        d1, d2 = datetime(2020, 1, 1), datetime(2021, 6, 15)
        conflicted = [
            PhotoInfo(path="C:/g1_a.jpg", date_taken=d1),
            PhotoInfo(path="C:/g1_b.jpg", date_taken=d2),
        ]
        clean = [
            PhotoInfo(path="C:/g2_a.jpg", date_taken=d1),
            PhotoInfo(path="C:/g2_b.jpg", date_taken=d1),
        ]
        catalog = _FakeMigrationCatalog({1: conflicted, 2: clean})
        t = _DupMigrationThread(catalog)
        counts = []
        t.done.connect(counts.append)

        t.run()

        assert catalog.ignored == [1]           # seul le groupe conflictuel
        cache = _FakeDedupCache.instances[0]
        assert cache.opened and cache.closed
        import os
        assert sorted(cache.removed) == [
            os.path.normpath("C:/g1_a.jpg"), os.path.normpath("C:/g1_b.jpg")
        ]
        assert counts == [1]                    # badge : 1 groupe restant

    def test_no_group_short_circuits(self, monkeypatch):
        _FakeDedupCache.instances = []
        monkeypatch.setattr(bw, "DedupCache", _FakeDedupCache)
        catalog = _FakeMigrationCatalog({})
        t = _DupMigrationThread(catalog)
        counts = []
        t.done.connect(counts.append)

        t.run()

        assert counts == [0]
        assert _FakeDedupCache.instances == []   # jamais instancié

    def test_migration_error_still_emits_count(self):
        class _Broken:
            def count_duplicate_groups(self):
                raise RuntimeError("boom")

        t = _DupMigrationThread(_Broken())
        counts = []
        t.done.connect(counts.append)

        t.run()

        assert counts == [0]


class TestPersonsRefreshThread:
    def test_loads_persons_and_cluster_count(self, tmp_path):
        face_db = FaceDatabase(db_path=tmp_path / "faces.db")
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.create_person("Alice")
        _insert_emb_face(face_db, "C:/c1.jpg", [1.0, 0.0], cluster_id=1)
        t = _PersonsRefreshThread(catalog, face_db)
        results = []
        t.result_ready.connect(lambda persons, n: results.append((persons, n)))

        t.run()

        persons, count = results[0]
        assert [p.name for p in persons] == ["Alice"]
        assert count == 1

    def test_error_emits_empty(self):
        t = _PersonsRefreshThread(None, None)
        results = []
        t.result_ready.connect(lambda persons, n: results.append((persons, n)))

        t.run()

        assert results == [([], 0)]


class TestResuggestThread:
    def test_run_delegates_to_face_db(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _insert_emb_face(db, "C:/c1.jpg", [1.0, 0.0], cluster_id=1)

        t = _ResuggestThread(db, [1], exclude_pid=None)
        t.run()   # sans personne connue : aucune suggestion, mais pas d'erreur

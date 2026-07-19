# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de _DeleteWorkerThread (main_window.py) : run() est appelé en
synchrone (pas de start(), pas de boucle d'événements) contre un Catalog,
un ThumbnailCache et une FaceDatabase temporaires — c'est la logique de purge
en lot qui est testée, pas le threading Qt."""
import os

from src.core.models import PhotoInfo
from src.faces.face_database import FaceDatabase
from src.library.catalog import Catalog
from src.library.thumbnail_cache import ThumbnailCache
from src.ui.main_window import _DeleteWorkerThread


def _make_env(tmp_path, names):
    """Crée des fichiers réels + un catalogue les référençant."""
    catalog = Catalog(db_path=tmp_path / "catalog.db")
    thumb_cache = ThumbnailCache(db_path=tmp_path / "thumbs.db")
    face_db = FaceDatabase(db_path=tmp_path / "faces.db")
    paths = []
    for name in names:
        p = tmp_path / name
        p.write_bytes(b"x")
        catalog.add_or_update_photo(PhotoInfo(path=str(p)))
        paths.append(str(p))
    return catalog, thumb_cache, face_db, paths


class TestDeleteWorker:
    def test_deletes_files_and_purges_catalog(self, qtbot, tmp_path):
        catalog, thumb_cache, face_db, paths = _make_env(
            tmp_path, ["a.jpg", "b.jpg", "keep.jpg"]
        )
        worker = _DeleteWorkerThread(paths[:2], catalog, thumb_cache, face_db)
        results = []
        worker.finished_delete.connect(lambda d, e: results.append((d, e)))

        worker.run()   # synchrone

        deleted, errors = results[0]
        assert deleted == paths[:2]
        assert errors == []
        assert not os.path.exists(paths[0]) and not os.path.exists(paths[1])
        assert os.path.exists(paths[2])
        assert catalog.get_photo_by_path(paths[0]) is None
        assert catalog.get_photo_by_path(paths[2]) is not None

    def test_dissolves_singleton_duplicate_group(self, qtbot, tmp_path):
        catalog, thumb_cache, face_db, paths = _make_env(tmp_path, ["a.jpg", "b.jpg"])
        catalog.set_duplicate_groups({paths[0]: 1, paths[1]: 1})
        assert catalog.count_duplicate_groups() == 1

        worker = _DeleteWorkerThread([paths[0]], catalog, thumb_cache, face_db)
        worker.run()

        # Le groupe réduit à 1 exemplaire est dissous par delete_photos
        assert catalog.count_duplicate_groups() == 0

    def test_purges_face_data(self, qtbot, tmp_path):
        import sqlite3

        catalog, thumb_cache, face_db, paths = _make_env(tmp_path, ["a.jpg"])
        norm = os.path.normpath(paths[0])
        face_db.save_faces(norm, [])   # marque la photo comme indexée

        def _indexed() -> bool:
            conn = sqlite3.connect(face_db._db_path)
            try:
                row = conn.execute(
                    "SELECT 1 FROM indexed_photos WHERE photo_path=?", (norm,)
                ).fetchone()
                return row is not None
            finally:
                conn.close()

        assert _indexed()

        worker = _DeleteWorkerThread([paths[0]], catalog, thumb_cache, face_db)
        worker.run()

        assert not _indexed()

    def test_missing_file_is_not_an_error(self, qtbot, tmp_path):
        """unlink(missing_ok=True) : un fichier déjà disparu du disque est
        quand même purgé du catalogue, sans erreur remontée."""
        catalog, thumb_cache, face_db, paths = _make_env(tmp_path, ["a.jpg"])
        os.remove(paths[0])

        worker = _DeleteWorkerThread([paths[0]], catalog, thumb_cache, face_db)
        results = []
        worker.finished_delete.connect(lambda d, e: results.append((d, e)))
        worker.run()

        deleted, errors = results[0]
        assert deleted == paths and errors == []
        assert catalog.get_photo_by_path(paths[0]) is None

    def test_progress_emitted_per_file(self, qtbot, tmp_path):
        catalog, thumb_cache, face_db, paths = _make_env(tmp_path, ["a.jpg", "b.jpg"])
        worker = _DeleteWorkerThread(paths, catalog, thumb_cache, face_db)
        ticks = []
        worker.progress.connect(lambda done, total: ticks.append((done, total)))

        worker.run()

        assert ticks == [(1, 2), (2, 2)]

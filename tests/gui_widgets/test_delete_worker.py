# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de _DeleteWorkerThread (main_window.py) : run() est appelé en
synchrone (pas de start(), pas de boucle d'événements) contre un Catalog,
un ThumbnailCache et une FaceDatabase temporaires — c'est la logique de purge
en lot qui est testée, pas le threading Qt.

La mise à la corbeille (src/library/trash.py) est simulée par une fixture
autouse : un vrai send2trash enverrait les fichiers de tmp_path dans la
corbeille Windows de l'utilisateur à chaque run de tests."""
import os

import pytest

import src.library.trash as trash_module
from src.core.models import PhotoInfo
from src.faces.face_database import FaceDatabase
from src.library.catalog import Catalog
from src.library.thumbnail_cache import ThumbnailCache
from src.ui.main_window import _DeleteWorkerThread


@pytest.fixture(autouse=True)
def fake_trash(monkeypatch):
    """Remplace move_to_trash par une suppression directe traçée (mêmes
    sémantiques d'erreur : FileNotFoundError si absent)."""
    calls: list[str] = []

    def _fake(path: str) -> None:
        norm = os.path.normpath(path)
        if not os.path.exists(norm):
            raise FileNotFoundError(norm)
        calls.append(norm)
        if os.path.isdir(norm):
            import shutil
            shutil.rmtree(norm)
        else:
            os.remove(norm)

    monkeypatch.setattr(trash_module, "move_to_trash", _fake)
    return calls


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
        """Un fichier déjà disparu du disque (FileNotFoundError de la
        corbeille) est quand même purgé du catalogue, sans erreur remontée —
        équivalent de l'ancien unlink(missing_ok=True)."""
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

    def test_trash_failure_keeps_file_and_catalog(self, qtbot, tmp_path, monkeypatch):
        """Régression corbeille : si la mise à la corbeille échoue (lecteur
        réseau, volume sans corbeille), le fichier reste INTACT sur le disque,
        le chemin part dans errors et le catalogue n'est PAS purgé — jamais de
        repli unlink silencieux."""
        catalog, thumb_cache, face_db, paths = _make_env(tmp_path, ["a.jpg", "b.jpg"])

        real_exists = os.path.exists

        def _failing(path):
            if os.path.normpath(path) == os.path.normpath(paths[0]):
                raise OSError("corbeille indisponible sur ce volume")
            if not real_exists(path):
                raise FileNotFoundError(path)
            os.remove(path)

        monkeypatch.setattr(trash_module, "move_to_trash", _failing)
        worker = _DeleteWorkerThread(paths, catalog, thumb_cache, face_db)
        results = []
        worker.finished_delete.connect(lambda d, e: results.append((d, e)))

        worker.run()

        deleted, errors = results[0]
        assert deleted == [paths[1]]
        assert len(errors) == 1
        assert "n'a PAS été supprimé" in errors[0]
        assert os.path.exists(paths[0])                       # fichier intact
        assert catalog.get_photo_by_path(paths[0]) is not None  # catalogue intact
        assert catalog.get_photo_by_path(paths[1]) is None

    def test_worker_goes_through_trash_module(self, qtbot, tmp_path, fake_trash):
        """Le worker passe bien par src.library.trash (point unique corbeille)."""
        catalog, thumb_cache, face_db, paths = _make_env(tmp_path, ["a.jpg"])

        worker = _DeleteWorkerThread(paths, catalog, thumb_cache, face_db)
        worker.run()

        assert fake_trash == [os.path.normpath(paths[0])]

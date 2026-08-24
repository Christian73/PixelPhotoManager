# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests of _DeleteWorkerThread (main_window.py): run() is called
synchronously (no start(), no event loop) against a temporary Catalog,
ThumbnailCache and FaceDatabase -- what is tested is the batch purge logic, not
the Qt threading.

Moving to the recycle bin (src/library/trash.py) is simulated by an autouse
fixture: a real send2trash would send the files of tmp_path into the user's
Windows recycle bin on every test run."""
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
    """Replaces move_to_trash with a traced direct deletion (same error
    semantics: FileNotFoundError if absent)."""
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
    """Creates real files + a catalog referencing them."""
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

        worker.run()   # synchronous

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

        # The group reduced to 1 copy is dissolved by delete_photos
        assert catalog.count_duplicate_groups() == 0

    def test_purges_face_data(self, qtbot, tmp_path):
        import sqlite3

        catalog, thumb_cache, face_db, paths = _make_env(tmp_path, ["a.jpg"])
        norm = os.path.normpath(paths[0])
        face_db.save_faces(norm, [])   # marks the photo as indexed

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
        """A file already gone from the disk (FileNotFoundError from the recycle
        bin) is purged from the catalog all the same, with no error reported --
        the equivalent of the former unlink(missing_ok=True)."""
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
        """Recycle bin regression: if moving to the recycle bin fails (network
        drive, volume without a recycle bin), the file stays INTACT on the disk,
        the path goes into errors and the catalog is NOT purged -- never a
        silent unlink fallback."""
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
        assert "the file was NOT deleted" in errors[0]
        assert os.path.exists(paths[0])                       # file intact
        assert catalog.get_photo_by_path(paths[0]) is not None  # catalog intact
        assert catalog.get_photo_by_path(paths[1]) is None

    def test_worker_goes_through_trash_module(self, qtbot, tmp_path, fake_trash):
        """The worker really goes through src.library.trash (the single recycle bin point)."""
        catalog, thumb_cache, face_db, paths = _make_env(tmp_path, ["a.jpg"])

        worker = _DeleteWorkerThread(paths, catalog, thumb_cache, face_db)
        worker.run()

        assert fake_trash == [os.path.normpath(paths[0])]

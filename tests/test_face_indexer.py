# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste `src/faces/face_indexer.py` sans InsightFace ni vrais sous-processus :
un ProcessPoolExecutor factice exécute les soumissions en synchrone dans le
thread appelant, et le détecteur est remplacé par une fonction injectée. Couvre
le pipeline FaceIndexThread (succès, timeout → erreur enregistrée + repli CPU,
indisponibilité InsightFace, exclusion des vidéos), les threads mono-photo
(SingleFaceReindexThread, ForceRedetectThread, RetryFaceIndexThread) et les
threads de réévaluation (RevaluateSizeIgnoredThread, SimilaritySearchThread)."""
import concurrent.futures
import os
import sqlite3

import pytest
from PIL import Image

from src.core.models import PhotoInfo
from src.faces import face_indexer
from src.faces.face_database import FaceDatabase
from src.library.catalog import Catalog


# ------------------------------------------------------------------ fakes


class _FakeFuture:
    def __init__(self, fn, args):
        self._fn, self._args = fn, args

    def result(self, timeout=None):
        return self._fn(*self._args)


class _FakeExecutor:
    """Exécute les soumissions en synchrone, dans le processus de test."""

    def __init__(self, *args, **kwargs):
        self._processes = {}

    def submit(self, fn, *args):
        return _FakeFuture(fn, args)

    def shutdown(self, wait=True, cancel_futures=False):
        pass


def _detection(bbox=(10, 10, 100, 100), score=0.9):
    return {"bbox": bbox, "embedding": [0.5] * 8, "det_score": score}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Catalog + FaceDatabase + executor factice + détecteur neutralisé."""
    monkeypatch.setattr(
        concurrent.futures, "ProcessPoolExecutor", _FakeExecutor
    )
    monkeypatch.setattr(face_indexer, "warmup_worker", lambda: None)
    monkeypatch.setattr(face_indexer, "warmup_worker_cpu", lambda: None)
    catalog = Catalog(db_path=tmp_path / "catalog.db")
    face_db = FaceDatabase(db_path=tmp_path / "faces.db")
    photos = tmp_path / "photos"
    photos.mkdir()
    return catalog, face_db, photos


def _add_photo(catalog, folder, name, size=(200, 200)) -> str:
    p = folder / name
    Image.new("RGB", size).save(str(p))
    path = os.path.normpath(str(p))
    catalog.add_or_update_photo(PhotoInfo(path=path, file_size=1, file_mtime=1.0))
    return path


def _run_thread(qtbot, thread, signal, timeout=15000):
    """Exécute run() en synchrone : signaux émis en connexion directe, et le
    code du thread est tracé par coverage (un .start() natif Qt échappe à
    sys.settrace)."""
    thread.run()


# ------------------------------------------------------------------ FaceIndexThread


class TestFaceIndexThread:
    def test_no_photos_finishes_immediately(self, qtbot, env):
        catalog, face_db, photos = env
        thread = face_indexer.FaceIndexThread(face_db, catalog)
        results: list = []
        thread.finished.connect(lambda a, b: results.append((a, b)))
        _run_thread(qtbot, thread, thread.finished)
        assert results == [(0, 0)]

    def test_successful_indexing(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        p1 = _add_photo(catalog, photos, "a.jpg")
        p2 = _add_photo(catalog, photos, "b.jpg")
        monkeypatch.setattr(
            face_indexer, "detect_and_embed_auto",
            lambda path: ([_detection()], 0),
        )

        thread = face_indexer.FaceIndexThread(face_db, catalog)
        indexed_photos: list = []
        results: list = []
        thread.photo_indexed.connect(lambda p, n: indexed_photos.append((p, n)))
        thread.finished.connect(lambda a, b: results.append((a, b)))
        _run_thread(qtbot, thread, thread.finished)

        assert results == [(2, 2)]
        assert {p for p, _ in indexed_photos} == {p1, p2}
        conn = sqlite3.connect(face_db._db_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 2
        finally:
            conn.close()

    def test_video_files_excluded(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        video = photos / "clip.mp4"
        video.write_bytes(b"x")
        catalog.add_or_update_photo(
            PhotoInfo(path=os.path.normpath(str(video)), file_size=1,
                      file_mtime=1.0, media_type="video")
        )
        monkeypatch.setattr(
            face_indexer, "detect_and_embed_auto",
            lambda path: ([_detection()], 0),
        )
        thread = face_indexer.FaceIndexThread(face_db, catalog)
        results: list = []
        thread.finished.connect(lambda a, b: results.append((a, b)))
        _run_thread(qtbot, thread, thread.finished)
        assert results == [(0, 0)]

    def test_timeout_records_error_and_continues(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        p_bad = _add_photo(catalog, photos, "bad.jpg")

        def _detect(path):
            raise concurrent.futures.TimeoutError()

        monkeypatch.setattr(face_indexer, "detect_and_embed_auto", _detect)

        thread = face_indexer.FaceIndexThread(face_db, catalog)
        errors: list = []
        results: list = []
        thread.error.connect(lambda p, m: errors.append((p, m)))
        thread.finished.connect(lambda a, b: results.append((a, b)))
        _run_thread(qtbot, thread, thread.finished)

        assert results == [(0, 0)]
        assert len(errors) == 1
        assert "timeout" in errors[0][1]
        err = face_db.get_index_error(p_bad)
        assert err is not None
        assert err["error_type"] == "timeout"

    def test_insightface_unavailable(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        _add_photo(catalog, photos, "a.jpg")

        def _detect(path):
            raise RuntimeError("insightface non installé")

        monkeypatch.setattr(face_indexer, "detect_and_embed_auto", _detect)

        thread = face_indexer.FaceIndexThread(face_db, catalog)
        unavailable: list = []
        thread.unavailable.connect(lambda: unavailable.append(True))
        _run_thread(qtbot, thread, thread.finished)
        assert unavailable == [True]

    def test_per_photo_error_continues(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        p1 = _add_photo(catalog, photos, "a.jpg")
        p2 = _add_photo(catalog, photos, "b.jpg")

        def _detect(path):
            if path == p1:
                raise ValueError("image corrompue")
            return [_detection()], 0

        monkeypatch.setattr(face_indexer, "detect_and_embed_auto", _detect)

        thread = face_indexer.FaceIndexThread(face_db, catalog)
        errors: list = []
        results: list = []
        thread.error.connect(lambda p, m: errors.append((p, m)))
        thread.finished.connect(lambda a, b: results.append((a, b)))
        _run_thread(qtbot, thread, thread.finished)

        assert results == [(1, 1)]
        assert errors == [(p1, "image corrompue")]

    def test_already_indexed_skipped(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        p1 = _add_photo(catalog, photos, "a.jpg")
        face_db.save_faces(p1, [_detection()])  # déjà indexée
        monkeypatch.setattr(
            face_indexer, "detect_and_embed_auto",
            lambda path: ([_detection()], 0),
        )
        thread = face_indexer.FaceIndexThread(face_db, catalog)
        results: list = []
        thread.finished.connect(lambda a, b: results.append((a, b)))
        _run_thread(qtbot, thread, thread.finished)
        assert results == [(0, 0)]


# ------------------------------------------------------------------ threads mono-photo


class TestSingleFaceReindexThread:
    def test_success(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        p = _add_photo(catalog, photos, "a.jpg")
        monkeypatch.setattr(
            face_indexer, "detect_and_embed",
            lambda path, rotation=0: [_detection()],
        )
        thread = face_indexer.SingleFaceReindexThread(face_db, p, rotation=90)
        results: list = []
        cluster_calls: list = []
        thread.finished.connect(lambda pp, n: results.append((pp, n)))
        thread.cluster_requested.connect(lambda: cluster_calls.append(True))
        _run_thread(qtbot, thread, thread.finished)
        assert results == [(p, 1)]
        assert cluster_calls == [True]

    def test_video_skipped(self, qtbot, env):
        catalog, face_db, photos = env
        thread = face_indexer.SingleFaceReindexThread(face_db, "clip.mp4")
        thread.run()  # retour immédiat, sans détection

    def test_error_emitted(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        p = _add_photo(catalog, photos, "a.jpg")

        def _detect(path, rotation=0):
            raise ValueError("boom")

        monkeypatch.setattr(face_indexer, "detect_and_embed", _detect)
        thread = face_indexer.SingleFaceReindexThread(face_db, p)
        errors: list = []
        thread.error.connect(lambda pp, m: errors.append((pp, m)))
        _run_thread(qtbot, thread, thread.error)
        assert errors == [(p, "boom")]

    def test_insightface_missing_silent(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        p = _add_photo(catalog, photos, "a.jpg")

        def _detect(path, rotation=0):
            raise RuntimeError("pas d'insightface")

        monkeypatch.setattr(face_indexer, "detect_and_embed", _detect)
        thread = face_indexer.SingleFaceReindexThread(face_db, p)
        thread.run()  # RuntimeError avalé silencieusement


class TestForceRedetectThread:
    def test_success_no_size_limit(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        p = _add_photo(catalog, photos, "a.jpg")
        # visage minuscule qui serait normalement auto-ignoré par taille
        monkeypatch.setattr(
            face_indexer, "detect_and_embed",
            lambda path, rotation=0: [_detection(bbox=(0, 0, 24, 24))],
        )
        thread = face_indexer.ForceRedetectThread(face_db, p)
        results: list = []
        thread.finished.connect(lambda pp, n: results.append((pp, n)))
        _run_thread(qtbot, thread, thread.finished)
        assert results == [(p, 1)]
        conn = sqlite3.connect(face_db._db_path)
        try:
            assert conn.execute(
                "SELECT ignored FROM faces WHERE photo_path=?", (p,)
            ).fetchone()[0] == 0   # force_no_limit → jamais ignoré
        finally:
            conn.close()

    def test_video_skipped(self, qtbot, env):
        catalog, face_db, photos = env
        thread = face_indexer.ForceRedetectThread(face_db, "clip.avi")
        thread.run()  # retour immédiat, sans détection


class TestRetryFaceIndexThread:
    def test_success_clears_error(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        p = _add_photo(catalog, photos, "a.jpg")
        face_db.mark_index_error(p, "timeout")
        monkeypatch.setattr(
            face_indexer, "detect_and_embed_auto",
            lambda path: ([_detection()], 0),
        )
        thread = face_indexer.RetryFaceIndexThread(face_db, p)
        results: list = []
        thread.finished.connect(lambda pp, ok, n: results.append((pp, ok, n)))
        _run_thread(qtbot, thread, thread.finished)
        assert results == [(p, True, 1)]
        assert face_db.get_index_error(p) is None   # succès efface l'erreur

    def test_new_timeout_keeps_error(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        p = _add_photo(catalog, photos, "a.jpg")

        def _detect(path):
            raise concurrent.futures.TimeoutError()

        monkeypatch.setattr(face_indexer, "detect_and_embed_auto", _detect)
        thread = face_indexer.RetryFaceIndexThread(face_db, p)
        results: list = []
        thread.finished.connect(lambda pp, ok, n: results.append((pp, ok, n)))
        _run_thread(qtbot, thread, thread.finished)
        assert results == [(p, False, 0)]
        assert face_db.get_index_error(p)["error_type"] == "timeout"


# ------------------------------------------------------------------ threads utilitaires


class TestUtilityThreads:
    def test_tf_warmup_thread_noop(self, qtbot):
        thread = face_indexer.TFWarmUpThread()
        thread.run()  # no-op de compatibilité

    def test_revaluate_size_ignored_thread(self, qtbot, env):
        catalog, face_db, photos = env
        thread = face_indexer.RevaluateSizeIgnoredThread(face_db)
        results: list = []
        thread.finished.connect(lambda a, b: results.append((a, b)))
        _run_thread(qtbot, thread, thread.finished)
        assert results == [(0, 0)]

    def test_similarity_search_thread(self, qtbot, env):
        catalog, face_db, photos = env
        thread = face_indexer.SimilaritySearchThread(face_db)
        results: list = []
        thread.finished.connect(lambda a, b: results.append((a, b)))
        _run_thread(qtbot, thread, thread.finished)
        assert results == [(0, 0)]

    def test_similarity_search_error_fallback(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env

        def _boom(progress_cb=None):
            raise RuntimeError("db corrompue")

        monkeypatch.setattr(face_db, "find_similar_to_persons", _boom)
        thread = face_indexer.SimilaritySearchThread(face_db)
        results: list = []
        thread.finished.connect(lambda a, b: results.append((a, b)))
        _run_thread(qtbot, thread, thread.finished)
        assert results == [(0, 0)]

    def test_kill_executor_tolerates_fake(self):
        face_indexer._kill_executor(_FakeExecutor())  # ne doit pas lever

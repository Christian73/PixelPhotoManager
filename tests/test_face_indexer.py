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


class _FakeEditDb:
    """Double d'EditDatabase pour ForceRedetectThread (jamais la vraie DB du
    profil utilisateur)."""

    def __init__(self, rotation: float = 0.0, raises: bool = False):
        self._rotation = rotation
        self._raises = raises

    def load(self, photo_path):
        if self._raises:
            raise sqlite3.OperationalError("database is locked")
        return type("_E", (), {"rotation": self._rotation})()


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

    def test_stale_indexed_rotation_no_longer_wins(self, qtbot, env, monkeypatch):
        """Régression du cas réel : photo indexée à 90° (rotation ensuite annulée
        par l'utilisateur sans resynchronisation de l'index). La détection forcée
        réutilisait aveuglément 90° et ne retrouvait que 2 visages sur 8, à chaque
        appel — impossible à débloquer depuis l'UI. Elle doit maintenant essayer
        aussi la rotation affichée (0°) et garder la plus fructueuse."""
        catalog, face_db, photos = env
        p = _add_photo(catalog, photos, "a.jpg")
        face_db.save_faces(p, [_detection()], rotation=90)
        assert face_db.get_indexed_rotation(p) == 90

        by_rotation = {0: [_detection()] * 8, 90: [_detection()] * 2}
        monkeypatch.setattr(
            face_indexer, "detect_and_embed",
            lambda path, rotation=0: by_rotation.get(rotation, []),
        )
        thread = face_indexer.ForceRedetectThread(
            face_db, p, edit_db=_FakeEditDb(rotation=0.0),
        )
        results: list = []
        thread.finished.connect(lambda pp, n: results.append((pp, n)))
        _run_thread(qtbot, thread, thread.finished)

        assert results == [(p, 8)]
        assert face_db.get_indexed_rotation(p) == 0

    def test_auto_fallback_rotation_is_preserved(self, qtbot, env, monkeypatch):
        """Cas inverse, légitime et fréquent : detect_and_embed_auto avait basculé
        sur 270° parce que 0° ne trouvait rien (photo prise de travers). Suivre
        aveuglément la rotation d'édition (0°) perdrait le visage — la rotation
        indexée doit rester candidate et l'emporter."""
        catalog, face_db, photos = env
        p = _add_photo(catalog, photos, "a.jpg")
        face_db.save_faces(p, [_detection()], rotation=270)

        by_rotation = {0: [], 270: [_detection()]}
        monkeypatch.setattr(
            face_indexer, "detect_and_embed",
            lambda path, rotation=0: by_rotation.get(rotation, []),
        )
        thread = face_indexer.ForceRedetectThread(
            face_db, p, edit_db=_FakeEditDb(rotation=0.0),
        )
        results: list = []
        thread.finished.connect(lambda pp, n: results.append((pp, n)))
        _run_thread(qtbot, thread, thread.finished)

        assert results == [(p, 1)]
        assert face_db.get_indexed_rotation(p) == 270

    def test_tie_prefers_displayed_rotation(self, qtbot, env, monkeypatch):
        """À égalité de visages trouvés, on garde le repère affiché (0°) : c'est
        lui qui sert à découper les vignettes de visage (detected_rotation)."""
        catalog, face_db, photos = env
        p = _add_photo(catalog, photos, "a.jpg")
        face_db.save_faces(p, [_detection()], rotation=180)

        monkeypatch.setattr(
            face_indexer, "detect_and_embed",
            lambda path, rotation=0: [_detection()],
        )
        thread = face_indexer.ForceRedetectThread(
            face_db, p, edit_db=_FakeEditDb(rotation=0.0),
        )
        _run_thread(qtbot, thread, thread.finished)

        assert face_db.get_indexed_rotation(p) == 0

    def test_single_detection_when_rotations_agree(self, qtbot, env, monkeypatch):
        """Cas nominal (les deux rotations coïncident) : une seule détection, pas
        de coût doublé."""
        catalog, face_db, photos = env
        p = _add_photo(catalog, photos, "a.jpg")
        face_db.save_faces(p, [_detection()], rotation=0)

        rotations: list = []

        def _detect(path, rotation=0):
            rotations.append(rotation)
            return [_detection()]

        monkeypatch.setattr(face_indexer, "detect_and_embed", _detect)
        thread = face_indexer.ForceRedetectThread(
            face_db, p, edit_db=_FakeEditDb(rotation=0.0),
        )
        _run_thread(qtbot, thread, thread.finished)

        assert rotations == [0]

    def test_falls_back_to_indexed_rotation_when_edits_unreadable(
        self, qtbot, env, monkeypatch
    ):
        catalog, face_db, photos = env
        p = _add_photo(catalog, photos, "a.jpg")
        face_db.save_faces(p, [_detection()], rotation=270)

        rotations: list = []

        def _detect(path, rotation=0):
            rotations.append(rotation)
            return [_detection()]

        monkeypatch.setattr(face_indexer, "detect_and_embed", _detect)
        thread = face_indexer.ForceRedetectThread(
            face_db, p, edit_db=_FakeEditDb(raises=True),
        )
        _run_thread(qtbot, thread, thread.finished)

        assert rotations == [270]


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


# ------------------------------------------------------------------ bridage CPU


class _CapturingExecutor(_FakeExecutor):
    """_FakeExecutor qui mémorise les kwargs de construction (initializer)."""

    created: list = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _CapturingExecutor.created.append(kwargs)


@pytest.fixture
def capturing_executor(monkeypatch):
    _CapturingExecutor.created = []
    monkeypatch.setattr(
        concurrent.futures, "ProcessPoolExecutor", _CapturingExecutor
    )
    return _CapturingExecutor


class TestBackgroundCpuThrottling:
    """L'indexation des visages tourne en continu et sans intervention de
    l'utilisateur : elle doit se plier au même bridage que la détection de
    doublons, sous peine de saturer la machine à elle seule."""

    def test_throttle_tick_once_per_photo(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        _add_photo(catalog, photos, "a.jpg")
        _add_photo(catalog, photos, "b.jpg")
        _add_photo(catalog, photos, "c.jpg")
        monkeypatch.setattr(
            face_indexer, "detect_and_embed_auto", lambda path: ([_detection()], 0),
        )
        ticks: list = []
        monkeypatch.setattr(
            face_indexer, "throttle_tick", lambda cancelled=None: ticks.append(cancelled),
        )

        thread = face_indexer.FaceIndexThread(face_db, catalog)
        _run_thread(qtbot, thread, thread.finished)

        assert len(ticks) == 3

    def test_tick_callback_reflects_the_stop_flag(self, qtbot, env, monkeypatch):
        """La pause doit rendre la main immédiatement à l'arrêt de
        l'application, sans attendre la fin du sommeil."""
        catalog, face_db, photos = env
        _add_photo(catalog, photos, "a.jpg")
        monkeypatch.setattr(
            face_indexer, "detect_and_embed_auto", lambda path: ([_detection()], 0),
        )
        ticks: list = []
        monkeypatch.setattr(
            face_indexer, "throttle_tick", lambda cancelled=None: ticks.append(cancelled),
        )

        thread = face_indexer.FaceIndexThread(face_db, catalog)
        _run_thread(qtbot, thread, thread.finished)

        assert len(ticks) == 1 and callable(ticks[0])
        assert ticks[0]() is False
        thread._stop_flag = True
        assert ticks[0]() is True

    def test_tick_happens_before_refilling_the_queue(self, qtbot, env, monkeypatch):
        """La pause est prise *avant* de réalimenter la file : c'est ce qui vide
        les sous-processus et bride réellement la charge, le travail ayant lieu
        hors de ce thread."""
        catalog, face_db, photos = env
        _add_photo(catalog, photos, "a.jpg")
        _add_photo(catalog, photos, "b.jpg")
        _add_photo(catalog, photos, "c.jpg")
        # Un seul worker : la file se vide et se remplit photo par photo, ce qui
        # rend l'alternance observable (avec _WORKERS > 1, _enqueue soumettrait
        # plusieurs photos d'un coup et l'ordre ne prouverait plus rien).
        monkeypatch.setattr(face_indexer, "_WORKERS", 1)
        order: list = []
        monkeypatch.setattr(
            face_indexer, "throttle_tick",
            lambda cancelled=None: order.append("tick"),
        )

        def _detect(path):
            order.append("submit")
            return [_detection()], 0

        monkeypatch.setattr(face_indexer, "detect_and_embed_auto", _detect)

        thread = face_indexer.FaceIndexThread(face_db, catalog)
        _run_thread(qtbot, thread, thread.finished)

        # _FakeExecutor exécute la détection au submit : "submit" marque donc le
        # moment où la file est réalimentée. Chaque tick doit précéder la
        # soumission suivante, jamais la suivre.
        assert order == ["submit", "tick", "submit", "tick", "submit", "tick"]

    def test_run_lowers_priority_and_caps_opencv(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        _add_photo(catalog, photos, "a.jpg")
        monkeypatch.setattr(
            face_indexer, "detect_and_embed_auto", lambda path: ([], 0),
        )
        calls: list = []
        monkeypatch.setattr(
            face_indexer, "lower_current_thread_priority",
            lambda: calls.append("priority"),
        )
        monkeypatch.setattr(
            face_indexer, "limit_cv2_threads", lambda n=1: calls.append(f"cv2:{n}"),
        )

        thread = face_indexer.FaceIndexThread(face_db, catalog)
        _run_thread(qtbot, thread, thread.finished)

        assert calls == ["priority", "cv2:1"]

    def test_subprocesses_use_the_background_initializer(
        self, qtbot, env, monkeypatch, capturing_executor,
    ):
        """Sans limite du pool interne d'OpenCV dans le worker, un seul
        sous-processus « throttlé » peut occuper tous les cœurs à lui seul."""
        catalog, face_db, photos = env
        _add_photo(catalog, photos, "a.jpg")
        monkeypatch.setattr(
            face_indexer, "detect_and_embed_auto", lambda path: ([], 0),
        )

        thread = face_indexer.FaceIndexThread(face_db, catalog)
        _run_thread(qtbot, thread, thread.finished)

        assert capturing_executor.created
        assert all(
            kwargs["initializer"] is face_indexer.init_background_process
            for kwargs in capturing_executor.created
        )

    def test_fallback_executors_use_the_background_initializer(
        self, env, capturing_executor,
    ):
        """Les executors de secours (repli CPU, retour GPU) sont créés sur des
        chemins distincts — faciles à oublier lors d'un changement d'initializer."""
        face_indexer._fresh_executor_cpu()
        face_indexer._fresh_executor_gpu()

        assert len(capturing_executor.created) >= 2
        assert all(
            kwargs["initializer"] is face_indexer.init_background_process
            for kwargs in capturing_executor.created
        )

# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests `src/faces/face_indexer.py` without InsightFace and without real subprocesses:
a fake ProcessPoolExecutor runs the submissions synchronously in the calling
thread, and the detector is replaced by an injected function. Covers the
FaceIndexThread pipeline (success, timeout -> error recorded + CPU fallback,
InsightFace unavailable, videos excluded), the single-photo threads
(SingleFaceReindexThread, ForceRedetectThread, RetryFaceIndexThread) and the
re-evaluation threads (RevaluateSizeIgnoredThread, SimilaritySearchThread)."""
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
    """Runs the submissions synchronously, in the test process."""

    def __init__(self, *args, **kwargs):
        self._processes = {}

    def submit(self, fn, *args):
        return _FakeFuture(fn, args)

    def shutdown(self, wait=True, cancel_futures=False):
        pass


def _detection(bbox=(10, 10, 100, 100), score=0.9):
    return {"bbox": bbox, "embedding": [0.5] * 8, "det_score": score}


class _FakeEditDb:
    """Stand-in for EditDatabase used by ForceRedetectThread (never the real DB of
    the user profile)."""

    def __init__(self, rotation: float = 0.0, raises: bool = False):
        self._rotation = rotation
        self._raises = raises

    def load(self, photo_path):
        if self._raises:
            raise sqlite3.OperationalError("database is locked")
        return type("_E", (), {"rotation": self._rotation})()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Catalog + FaceDatabase + fake executor + detector neutralised."""
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
    """Runs run() synchronously: signals emitted through direct connections, and
    the thread code is traced by coverage (a native Qt .start() escapes
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
        face_db.save_faces(p1, [_detection()])  # already indexed
        monkeypatch.setattr(
            face_indexer, "detect_and_embed_auto",
            lambda path: ([_detection()], 0),
        )
        thread = face_indexer.FaceIndexThread(face_db, catalog)
        results: list = []
        thread.finished.connect(lambda a, b: results.append((a, b)))
        _run_thread(qtbot, thread, thread.finished)
        assert results == [(0, 0)]


# ------------------------------------------------------------------ single-photo threads


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
        thread.run()  # immediate return, no detection

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
        thread.run()  # RuntimeError swallowed silently


class TestForceRedetectThread:
    def test_success_no_size_limit(self, qtbot, env, monkeypatch):
        catalog, face_db, photos = env
        p = _add_photo(catalog, photos, "a.jpg")
        # tiny face that would normally be auto-ignored by size
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
            ).fetchone()[0] == 0   # force_no_limit -> never ignored
        finally:
            conn.close()

    def test_video_skipped(self, qtbot, env):
        catalog, face_db, photos = env
        thread = face_indexer.ForceRedetectThread(face_db, "clip.avi")
        thread.run()  # immediate return, no detection

    def test_stale_indexed_rotation_no_longer_wins(self, qtbot, env, monkeypatch):
        """Regression of the real case: photo indexed at 90 degrees (rotation later
        cancelled by the user without resynchronising the index). Forced detection
        blindly reused 90 degrees and only found 2 faces out of 8, on every call --
        impossible to unblock from the UI. It must now also try the displayed
        rotation (0 degrees) and keep the most fruitful one."""
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
        """The opposite case, legitimate and frequent: detect_and_embed_auto had
        switched to 270 degrees because 0 degrees found nothing (photo taken
        sideways). Blindly following the edit rotation (0 degrees) would lose the
        face -- the indexed rotation must stay a candidate and win."""
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
        """With an equal number of faces found, the displayed reference (0 degrees)
        is kept: that is the one used to crop the face thumbnails
        (detected_rotation)."""
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
        """Nominal case (both rotations coincide): a single detection, no doubled
        cost."""
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
        assert face_db.get_index_error(p) is None   # success clears the error

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


# ------------------------------------------------------------------ utility threads


class TestUtilityThreads:
    def test_tf_warmup_thread_noop(self, qtbot):
        thread = face_indexer.TFWarmUpThread()
        thread.run()  # compatibility no-op

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
        face_indexer._kill_executor(_FakeExecutor())  # must not raise


# ------------------------------------------------------------------ CPU throttling


class _CapturingExecutor(_FakeExecutor):
    """_FakeExecutor that memorises the construction kwargs (initializer)."""

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
    """Face indexing runs continuously and without any user intervention: it
    must obey the same throttling as duplicate detection, otherwise it would
    saturate the machine all on its own."""

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
        """The pause must hand control back immediately when the application
        stops, without waiting for the end of the sleep."""
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
        """The pause is taken *before* refilling the queue: that is what empties
        the subprocesses and really throttles the load, the work taking place
        outside this thread."""
        catalog, face_db, photos = env
        _add_photo(catalog, photos, "a.jpg")
        _add_photo(catalog, photos, "b.jpg")
        _add_photo(catalog, photos, "c.jpg")
        # A single worker: the queue empties and refills photo by photo, which
        # makes the alternation observable (with _WORKERS > 1, _enqueue would
        # submit several photos at once and the order would no longer prove anything).
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

        # _FakeExecutor runs the detection at submit time: "submit" therefore marks
        # the moment the queue is refilled. Each tick must precede the next
        # submission, never follow it.
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
        """Without limiting the internal OpenCV pool inside the worker, a single
        "throttled" subprocess can occupy every core on its own."""
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
        """The fallback executors (CPU fallback, return to GPU) are created on
        distinct paths -- easy to forget when changing an initializer."""
        face_indexer._fresh_executor_cpu()
        face_indexer._fresh_executor_gpu()

        assert len(capturing_executor.created) >= 2
        assert all(
            kwargs["initializer"] is face_indexer.init_background_process
            for kwargs in capturing_executor.created
        )

# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests `src/faces/clusterer.py` with synthetic embeddings (no AI model at
all): _purify_clusters (splitting the inconsistent clusters), _fmt_n, the
_clustering_worker_proc worker called directly with a fake pipe, _run_clustering
end to end (a real multiprocessing subprocess + a real FaceDatabase), and the
ClusterThread wrapper (progress/finished/error signals)."""
import sqlite3

import numpy as np
import pytest

from src.faces import clusterer
from src.faces.face_database import FaceDatabase, _enc


def _vec(center: int, dim: int = 8, noise: float = 0.02, seed: int = 0) -> list[float]:
    rng = np.random.RandomState(seed)
    v = np.full(dim, 0.01, dtype=np.float32)
    v[center % dim] = 1.0
    v += rng.uniform(-noise, noise, dim).astype(np.float32)
    return v.tolist()


def _insert_emb_face(db, photo, emb, person_id=None, cluster_id=None) -> int:
    conn = sqlite3.connect(db._db_path)
    try:
        cur = conn.execute(
            "INSERT INTO faces (photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
            " embedding, person_id, cluster_id, ignored, pinned)"
            " VALUES (?,0,0,50,50,?,?,?,0,0)",
            (photo, _enc(emb), person_id, cluster_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _reset_sentinel(monkeypatch):
    """_last_clustered_n is a module global: reset it to -1 for every test."""
    monkeypatch.setattr(clusterer, "_last_clustered_n", -1)


# ------------------------------------------------------------------ _fmt_n


class TestFmtN:
    def test_thousands_separator(self):
        assert clusterer._fmt_n(1234567) == f"1{clusterer._NB_SP}234{clusterer._NB_SP}567"

    def test_small_number(self):
        assert clusterer._fmt_n(42) == "42"


# ------------------------------------------------------------------ _purify_clusters


def _norm_rows(X):
    X = np.asarray(X, dtype=np.float32)
    return X / np.linalg.norm(X, axis=1, keepdims=True)


class TestPurifyClusters:
    def test_coherent_cluster_untouched(self):
        X = _norm_rows([_vec(0, seed=i) for i in range(4)])
        labels = clusterer._purify_clusters(X, np.array([0, 0, 0, 0]))
        assert set(labels) == {0}

    def test_mixed_cluster_split(self):
        """Two orthogonal packs chained inside the same HDBSCAN label -> split."""
        X = _norm_rows(
            [_vec(0, seed=1), _vec(0, seed=2), _vec(1, seed=3), _vec(1, seed=4)]
        )
        labels = clusterer._purify_clusters(X, np.array([0, 0, 0, 0]))
        assert labels[0] == labels[1]
        assert labels[2] == labels[3]
        assert labels[0] != labels[2]

    def test_noise_labels_ignored(self):
        X = _norm_rows([_vec(0), _vec(1)])
        labels = clusterer._purify_clusters(X, np.array([-1, -1]))
        assert list(labels) == [-1, -1]

    def test_singleton_cluster_skipped(self):
        X = _norm_rows([_vec(0)])
        labels = clusterer._purify_clusters(X, np.array([0]))
        assert list(labels) == [0]

    def test_oversized_cluster_skipped(self, monkeypatch):
        monkeypatch.setattr(clusterer, "_PURITY_MAX_CLUSTER_N", 3)
        # 4 inconsistent faces but > _PURITY_MAX_CLUSTER_N -> left as they are
        X = _norm_rows(
            [_vec(0, seed=1), _vec(0, seed=2), _vec(1, seed=3), _vec(1, seed=4)]
        )
        labels = clusterer._purify_clusters(X, np.array([0, 0, 0, 0]))
        assert set(labels) == {0}

    def test_empty_input(self):
        labels = clusterer._purify_clusters(
            np.empty((0, 8), dtype=np.float32), np.array([], dtype=int)
        )
        assert len(labels) == 0


# ------------------------------------------------------------------ in-process worker


class _FakeConn:
    def __init__(self):
        self.messages: list[tuple] = []
        self.closed = False

    def send(self, msg):
        self.messages.append(msg)

    def close(self):
        self.closed = True


class TestClusteringWorker:
    def test_two_groups_and_singleton(self):
        """2 packs of 3 + 1 isolated -> 2 clusters, 1 relabelled singleton."""
        vecs = (
            [_vec(0, seed=i) for i in range(3)]
            + [_vec(1, seed=i + 10) for i in range(3)]
            + [_vec(4, seed=99)]
        )
        X = np.asarray(vecs, dtype=np.float32)
        conn = _FakeConn()
        clusterer._clustering_worker_proc(X.tobytes(), 7, 8, conn)

        assert conn.closed
        tags = [m[0] for m in conn.messages]
        assert tags == ["hdbscan", "result"]   # dim 8 < 32 -> no PCA
        _, n_clusters, n_singletons, labels = conn.messages[-1]
        assert n_clusters == 2
        assert n_singletons == 1
        assert labels[0] == labels[1] == labels[2]
        assert labels[3] == labels[4] == labels[5]
        assert labels[0] != labels[3]
        assert labels[6] not in (labels[0], labels[3])

    def test_pca_message_for_high_dim(self):
        """dim 64 > _PCA_DIMS and n > _PCA_DIMS -> PCA step emitted."""
        rng = np.random.RandomState(0)
        n = 40
        X = rng.rand(n, 64).astype(np.float32)
        conn = _FakeConn()
        clusterer._clustering_worker_proc(X.tobytes(), n, 64, conn)
        tags = [m[0] for m in conn.messages]
        assert tags[0] == "pca"
        assert "result" in tags

    def test_error_reported(self):
        conn = _FakeConn()
        # n*d inconsistent with the buffer size -> exception -> error message
        clusterer._clustering_worker_proc(b"\x00" * 8, 5, 8, conn)
        assert conn.messages[-1][0] == "error"
        assert conn.closed


# ------------------------------------------------------------------ _run_clustering


class TestRunClustering:
    def test_no_faces_returns_zero(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        assert clusterer._run_clustering(db) == 0

    def test_single_face_gets_cluster_zero(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        f = _insert_emb_face(db, "a.jpg", _vec(0))
        assert clusterer._run_clustering(db) == 1
        conn = sqlite3.connect(db._db_path)
        try:
            assert conn.execute(
                "SELECT cluster_id FROM faces WHERE id=?", (f,)
            ).fetchone()[0] == 0
        finally:
            conn.close()

    def test_full_run_with_subprocess(self, tmp_path):
        """Real subprocess: 2 groups of 3 faces -> 2 clusters in the database."""
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        g1 = [_insert_emb_face(db, f"a{i}.jpg", _vec(0, seed=i)) for i in range(3)]
        g2 = [_insert_emb_face(db, f"b{i}.jpg", _vec(1, seed=i + 10)) for i in range(3)]

        msgs: list[str] = []
        n = clusterer._run_clustering(db, progress_cb=msgs.append)

        assert n == 2
        conn = sqlite3.connect(db._db_path)
        try:
            cids = {
                fid: conn.execute(
                    "SELECT cluster_id FROM faces WHERE id=?", (fid,)
                ).fetchone()[0]
                for fid in g1 + g2
            }
        finally:
            conn.close()
        assert len({cids[f] for f in g1}) == 1
        assert len({cids[f] for f in g2}) == 1
        assert cids[g1[0]] != cids[g2[0]]
        assert any("HDBSCAN" in m for m in msgs)

    def test_skip_when_unchanged(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        for i in range(3):
            _insert_emb_face(db, f"a{i}.jpg", _vec(0, seed=i))
            _insert_emb_face(db, f"b{i}.jpg", _vec(1, seed=i + 10))
        first = clusterer._run_clustering(db)
        assert first >= 1
        # same N of unidentified faces, no synthetic assignment
        assert clusterer._run_clustering(db) == 0

    def test_timeout_kills_subprocess(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clusterer, "_CLUSTER_TIMEOUT", 0)
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        for i in range(3):
            _insert_emb_face(db, f"a{i}.jpg", _vec(0, seed=i))
        assert clusterer._run_clustering(db) == 0


# ------------------------------------------------------------------ ClusterThread


class TestResetClusteringCache:
    def test_clears_sentinel(self, monkeypatch):
        monkeypatch.setattr(clusterer, "_last_clustered_n", 42)
        clusterer.reset_clustering_cache()
        assert clusterer._last_clustered_n == -1

    def test_reclusters_after_db_reset_with_same_face_count(self, tmp_path):
        """Bug 2026-07 (test_faces_reset_full): FaceDatabase.reset_clustering()/
        reset_index() empty cluster_id in bulk without changing N (the same
        library reindexed identically) -- without invalidating the cache,
        _run_clustering believes nothing has changed and skips the grouping,
        leaving every face stuck with cluster_id=NULL indefinitely."""
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        ids = [
            _insert_emb_face(db, f"a{i}.jpg", _vec(0, seed=i)) for i in range(3)
        ] + [
            _insert_emb_face(db, f"b{i}.jpg", _vec(1, seed=i + 10)) for i in range(3)
        ]
        assert clusterer._run_clustering(db) >= 1

        db.reset_clustering()
        clusterer.reset_clustering_cache()
        assert clusterer._run_clustering(db) >= 1

        conn = sqlite3.connect(db._db_path)
        try:
            cluster_ids = [
                conn.execute(
                    "SELECT cluster_id FROM faces WHERE id=?", (fid,)
                ).fetchone()[0]
                for fid in ids
            ]
        finally:
            conn.close()
        assert all(cid is not None for cid in cluster_ids)


class TestClusterThread:
    def test_finished_signal(self, qtbot, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        for i in range(3):
            _insert_emb_face(db, f"a{i}.jpg", _vec(0, seed=i))
            _insert_emb_face(db, f"b{i}.jpg", _vec(1, seed=i + 10))
        thread = clusterer.ClusterThread(db)
        results: list[int] = []
        thread.finished.connect(results.append)
        thread.run()  # synchronous: direct signals + code traced by coverage
        assert results == [2]

    def test_error_signal(self, qtbot, tmp_path, monkeypatch):
        def _boom(face_db, progress_cb=None):
            raise RuntimeError("dépendance manquante")

        monkeypatch.setattr(clusterer, "_run_clustering", _boom)
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        thread = clusterer.ClusterThread(db)
        errors: list[str] = []
        thread.error.connect(errors.append)
        thread.run()  # synchronous: direct signals + code traced by coverage
        assert errors == ["dépendance manquante"]

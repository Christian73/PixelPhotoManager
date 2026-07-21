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
